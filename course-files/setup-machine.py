#!/usr/bin/env python3
"""Stand up the Viam 102 machine: create it, configure it, and run it.

Viam 102 needs a machine carrying the work cell Viam 101 built: a simulated arm,
a vacuum gripper, a floor, and the `viam102-workcell` fragment that supplies the
pick-station, the pallet, and the two scene services.

    python3 setup-machine.py --location <location>

In one command this:

  1. creates the machine (or reuses one of the same name),
  2. pushes the work-cell configuration to its main part,
  3. fetches the part secret and writes viam-server's cloud config,
  4. starts viam-server against it, supervised, and waits for it to serve.

It prints the part ID that `viam module reload-local` takes.

You must be logged in first (`viam login`): the machine is created in *your*
organization, because the module you publish later is private to it.

Requires: the `viam` CLI, the Viam Python SDK, and a viam-server binary (the
session pre-downloads one; pass --viam-server to point at another).
"""

import argparse
import asyncio
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

import term

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "workcell-config.json")


def viam(args):
    p = subprocess.run(["viam"] + list(args), capture_output=True, text=True)
    if p.returncode != 0:
        err = p.stderr or p.stdout
        # The CLI answers an ambiguous location name by dumping every matching
        # location, secrets included. Do not relay that to the terminal.
        if "Multiple locations match" in err:
            sys.exit("that location name exists in more than one organization.\n"
                     "Run `viam locations list` and pass the location's id "
                     "instead of its name, or add --org.")
        sys.exit(f"`viam {' '.join(args)}` failed:\n{err}")
    return p.stdout


COURSE_LOCATION = "viam-education"


def resolve_org(explicit=None):
    """Return (org_id, org_name) for the organization to work in.

    Signing up for Viam creates one organization named after you, so a learner
    who just signed up has exactly that one and nothing needs asking. No field
    on an organization marks it as the personal one, so when somebody belongs
    to several (anyone invited to a colleague's organization does) this asks
    rather than guessing and building the machine somewhere unexpected.
    """
    out = viam(["organizations", "list"])
    orgs = re.findall(r"^\s+(.+?) \(id: (\S+?)\)", out, re.M)
    if not orgs:
        sys.exit("no organizations found for this account; is `viam login` current?")

    if explicit:
        for name, oid in orgs:
            if explicit in (oid, name):
                return oid, name
        sys.exit(f"no organization matching {explicit!r}. Yours:\n" +
                 "\n".join(f"  {n} ({i})" for n, i in orgs))

    if len(orgs) == 1:
        return orgs[0][1], orgs[0][0]

    return choose_org(orgs)


def choose_org(orgs):
    """Ask which organization to use.

    Falls back to printing the --org form when there is nothing to read from,
    so an unattended run says what it needed rather than hanging on input.
    """
    print("You belong to more than one organization. Which one is this course for?")
    for n, (name, oid) in enumerate(orgs, 1):
        print(f"  {n}) {name}  ({oid})")

    for _ in range(3):
        try:
            answer = input("number, or an id: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit("no answer given. Run it again with the one you want:\n" +
                     "\n".join(f"  --org {i}   {n}" for n, i in orgs))
        if answer.isdigit() and 1 <= int(answer) <= len(orgs):
            name, oid = orgs[int(answer) - 1]
            return oid, name
        for name, oid in orgs:
            if answer in (oid, name):
                return oid, name
        print(f"  not one of them: {answer!r}")

    sys.exit("no valid choice after three tries; pass --org <id> instead.")


def resolve_location(org_id, org_name):
    """Return the id of the course's location, creating it the first time.

    Everything this course makes lives in one location, so it is easy to find
    and easy to delete afterwards.
    """
    for line in viam(["locations", "list", "--organization", org_id]).splitlines():
        m = re.match(rf"\s*{re.escape(COURSE_LOCATION)} \(id: (\S+)\)", line)
        if m:
            print(f"location  : {COURSE_LOCATION} ({m.group(1)})")
            return m.group(1)

    loc_id = asyncio.run(create_location(org_id))
    print(f"location  : {COURSE_LOCATION} ({loc_id}, created)")
    return loc_id


async def create_location(org_id):
    """Create the course location through the app API.

    The CLI has no `locations create`, and the app API needs an
    organization-scoped key, so one is made for this single call and deleted in
    the `finally`. It is only ever needed on a first run: once the location
    exists, `viam locations list` finds it with no key at all.
    """
    from viam.app.viam_client import ViamClient
    from viam.rpc.dial import DialOptions, Credentials

    raw = viam(["organizations", "api-key", "create", "--org-id", org_id,
                "--name", "viam-102-location-setup"])
    kid = re.search(r"Key ID:\s*(\S+)", raw)
    kval = re.search(r"Key Value:\s*(\S+)", raw)
    if not (kid and kval):
        sys.exit("could not read the new API key from the CLI output; create a "
                 f"location named {COURSE_LOCATION} in the app, then run this again")
    key_id, key_val = kid.group(1), kval.group(1)

    client = await ViamClient.create_from_dial_options(DialOptions(
        credentials=Credentials(type="api-key", payload=key_val), auth_entity=key_id))
    try:
        loc = await client.app_client.create_location(org_id=org_id,
                                                      name=COURSE_LOCATION)
        return loc.id
    finally:
        try:
            await client.app_client.delete_key(key_id)
        except Exception:
            print(f"note: could not revoke the setup key {key_id}; "
                  "delete it in the app under organization settings")
        client.close()


def find_machine(name, org, location):
    return pick_machine(list_machines(org, location), name)


def list_machines(org, location):
    """[(name, machine id, main part id)] for the machines in the location.

    A list, not a dict by name: machine names need not be unique, and a
    duplicate must not hide the machine it shares a name with.
    """
    args = ["machines", "list"]
    if org:
        args += ["--organization", org]
    if location:
        args += ["--location", location]
    found = []
    for line in viam(args).splitlines():
        m = re.match(r"\s*(.+?) \(id: (\S+)\) \(main part id: (\S+)\)", line)
        if m:
            found.append(m.groups())
    return found


def pick_machine(machines, name, saved_id=None, served=None):
    """(machine id, part id) of the machine called `name`, or (None, None).

    With several of that name, this session's own comes first.
    """
    named = [m for m in machines if m[0] == name]
    ours = [m for m in named if m[1] == saved_id or m[2] == served]
    chosen = (ours or named or [(None, None, None)])[0]
    return chosen[1], chosen[2]


DEFAULT_MACHINE = "viam-102"


def read_saved_env():
    """The values ~/.viam-102-env holds, or {} before the first run."""
    path = os.path.expanduser("~/.viam-102-env")
    values = {}
    if os.path.exists(path):
        for line in open(path):
            if line.startswith("export ") and "=" in line:
                k, v = line[len("export "):].split("=", 1)
                values[k.strip()] = _unquote(v)
    return values


def _unquote(value):
    """The value of an `export KEY=<value>` line, as the shell reads it."""
    try:
        words = shlex.split(value)
    except ValueError:
        return value.strip().strip('"')
    return words[0] if words else ""


def choose_machine_name(machines, saved_id, served=None, ask=input):
    """The name of the machine this session should set up.

    Setting up a machine replaces its configuration and starts viam-server as
    its main part, so only this session's own machine may be reused: the one
    whose id ~/.viam-102-env saved, or whose part viam-server's config in
    ~/viam-102-machine already names (a home restored from before the env
    file held the machine id). Another `viam-102` in the location belongs to
    a teammate in a shared organization, or to this learner's earlier
    session, which comes back when they return. Taking it over would break
    that one, so the default becomes the next free name instead.
    """
    for name, machine_id, part_id in machines:
        if (saved_id and machine_id == saved_id) or (served and part_id == served):
            return name
    names = {m[0] for m in machines}
    if DEFAULT_MACHINE not in names:
        return DEFAULT_MACHINE

    suggestion = next(f"{DEFAULT_MACHINE}-{i}" for i in range(2, 1000)
                      if f"{DEFAULT_MACHINE}-{i}" not in names)
    print(term.yellow(f"A machine named {DEFAULT_MACHINE} is already in "
                      f"{COURSE_LOCATION}, and it is not the one this session "
                      "set up."))
    print("Setting it up again would replace its configuration, and it may be a")
    print("teammate's in this organization or yours from an earlier session, so")
    print("use a different machine name. If it is yours and nothing else uses it,")
    print(f"type {DEFAULT_MACHINE} to reuse it.")
    try:
        picked = ask(f"Machine name [{suggestion}]: ").strip()
    except EOFError:
        print()
        picked = ""
    except KeyboardInterrupt:
        print()
        sys.exit("stopped; nothing was changed. Run it again to pick a name, "
                 "or pass --name.")
    return picked or suggestion


async def push_config_and_secret(machine_id, part_id, config_path):
    """Push the work-cell config and return the part secret viam-server needs."""
    from viam.app.viam_client import ViamClient
    from viam.rpc.dial import DialOptions, Credentials

    raw = viam(["machines", "api-key", "create", "--machine-id", machine_id,
                "--name", "viam-102-setup"])
    kid = re.search(r"Key ID:\s*(\S+)", raw)
    kval = re.search(r"Key Value:\s*(\S+)", raw)
    if not (kid and kval):
        sys.exit("could not read the new API key from the CLI output; "
                 "run `viam machines api-key create` yourself and pass --api-key")
    key_id, key_val = kid.group(1), kval.group(1)

    client = await ViamClient.create_from_dial_options(DialOptions(
        credentials=Credentials(type="api-key", payload=key_val), auth_entity=key_id))
    try:
        part = await client.app_client.get_robot_part(part_id)
        cfg = json.load(open(config_path))
        await client.app_client.update_robot_part(
            robot_part_id=part_id, name=part.name, robot_config=cfg)
        print(f"configured: {len(cfg.get('components', []))} components, "
              f"{len(cfg.get('modules', []))} modules, "
              f"{len(cfg.get('fragments', []))} fragments")
        secret = getattr(part, "secret", "") or ""
        if not secret:
            sys.exit("the machine part has no secret; check the API key's permissions")
        return secret
    finally:
        # The key existed only for this setup; do not leave it behind.
        try:
            await client.app_client.delete_key(key_id)
        except Exception:
            print(f"note: could not revoke the setup key {key_id}; "
                  "delete it in the app under organization settings")
        client.close()


def find_free_port(start=8080, end=8100):
    """viam-server binds :8080 by default, which is taken if one already runs."""
    import socket
    for port in range(start, end):
        with socket.socket() as sock:
            try:
                sock.bind(("", port))
                return port
            except OSError:
                continue
    sys.exit(f"no free port between {start} and {end} for viam-server")


def write_server_config(part_id, secret, workdir, port):
    """Write viam-server's cloud config. It holds a secret, so mode 600."""
    os.makedirs(workdir, exist_ok=True)
    path = os.path.join(workdir, "viam-server.json")
    with open(path, "w") as f:
        json.dump({"cloud": {"app_address": "https://app.viam.com:443",
                             "id": part_id, "secret": secret},
                   "network": {"bind_address": f":{port}"}}, f, indent=2)
    os.chmod(path, 0o600)
    print(f"wrote {path} (secret length {len(secret)}, mode 600, port {port})")
    return path


def find_viam_server(explicit=None):
    for c in (explicit, os.path.expanduser("~/viam-server"),
              "/opt/viam/bin/viam-server", shutil.which("viam-server")):
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    sys.exit("no viam-server binary found; pass --viam-server <path>")


def running_servers(config_path):
    """PIDs of this user's viam-server processes already started with this config.

    The session starts viam-server by itself for a returning learner
    (workshop/persist/viam-server-ctl.sh), so re-running setup could otherwise
    launch a second copy, and two would compete for the same machine
    credentials. Matched on the -config each was given, not on the name.
    """
    want = os.path.realpath(config_path)
    uid = os.getuid()
    pids = []
    entries = os.listdir("/proc") if os.path.isdir("/proc") else []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
            cwd = os.readlink(f"/proc/{entry}/cwd")
        except OSError:
            continue
        # argv[0] for the binary; argv[1] when an interpreter runs it.
        if not any(os.path.basename(a) == "viam-server" for a in argv[:2]):
            continue
        for i, arg in enumerate(argv):
            value = None
            if arg in ("-config", "--config") and i + 1 < len(argv):
                value = argv[i + 1]
            elif arg.startswith(("-config=", "--config=")):
                value = arg.split("=", 1)[1]
            if value and os.path.realpath(os.path.join(cwd, value)) == want:
                pids.append(int(entry))
                break
    return pids


def served_part(workdir):
    """The part id viam-server's config in `workdir` names, or None."""
    try:
        with open(os.path.join(workdir, "viam-server.json")) as f:
            return json.load(f)["cloud"]["id"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def must_restart(running, old_part, new_part):
    """Whether a running viam-server serves a part other than the new one.

    A server keeps the part it started with after its config file is
    rewritten, so left alone it would keep serving the old machine while the
    new one stays offline and VIAM_102_PART_ID names the new part.
    """
    return bool(running) and bool(old_part) and old_part != new_part


def stop_servers(pids, grace=10):
    """TERM, then KILL whatever is still up `grace` seconds later, as
    viam-server-ctl.sh's stop_running does."""
    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + grace
    while time.time() < deadline and any(alive(p) for p in pids):
        time.sleep(0.5)
    for pid in pids:
        if alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def log_says_serving(text):
    """True if viam-server logged that it is serving.

    RDK writes tab-separated lines whose message field is exactly `serving` when the
    web server is up (rdk robot/web/web.go). Match that field, not the substring:
    `error serving http` contains the word too, and reporting a failed start as
    online is the one answer this wait exists to avoid.
    """
    return any("serving" in line.split("\t") for line in text.splitlines())


def start_server(binary, config_path, log_path):
    """Launch viam-server and wait for it to report that it is serving.

    The log is appended to across runs, so only what this launch writes counts:
    scanning the whole file would match a `serving` line from an earlier run and
    report success before the server had said anything.
    """
    start_at = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    log = open(log_path, "a")
    proc = subprocess.Popen([binary, "-config", config_path],
                            stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    print(f"{term.bold('starting viam-server')} (pid {proc.pid}), "
          f"logging to {log_path}")
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit(f"viam-server exited early; see {log_path}")
        with open(log_path) as fh:
            fh.seek(start_at)
            if log_says_serving(fh.read()):
                print(term.green("viam-server is online"))
                return proc
        time.sleep(2)
    print(term.yellow(f"viam-server did not report 'serving' within 120s; "
                      f"check {log_path}"))
    return proc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", help=f"machine name (default: {DEFAULT_MACHINE}, "
                    "or a free name when another session's machine has it)")
    ap.add_argument("--location",
                    help=f"location name or id (default: {COURSE_LOCATION}, created "
                         "if missing)")
    ap.add_argument("--org", help="organization id or name (default: your only one)")
    ap.add_argument("--config", default=CONFIG, help="work-cell config JSON")
    ap.add_argument("--workdir", default=os.path.expanduser("~/viam-102-machine"),
                    help="where the server config and log are written")
    ap.add_argument("--viam-server", dest="viam_server", help="viam-server binary")
    ap.add_argument("--port", type=int,
                    help="viam-server bind port (default: first free from 8080)")
    ap.add_argument("--no-start", action="store_true",
                    help="configure the machine but do not launch viam-server")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        sys.exit(f"work-cell config not found: {args.config}")

    org_id, org_name = resolve_org(args.org)
    print(f"org       : {org_name} ({org_id})")
    location = args.location or resolve_location(org_id, org_name)

    machines = list_machines(org_id, location)
    saved_id = read_saved_env().get("VIAM_102_MACHINE_ID")
    old_part = served_part(args.workdir)
    args.name = args.name or choose_machine_name(machines, saved_id, old_part)
    machine_id, part_id = pick_machine(machines, args.name, saved_id, old_part)
    if machine_id:
        print(f"reusing machine {args.name} ({machine_id})")
    else:
        viam(["machines", "create", "--name", args.name,
              "--location", location, "--organization", org_id])
        known = {m[1] for m in machines}
        machine_id, part_id = pick_machine(
            [m for m in list_machines(org_id, location) if m[1] not in known],
            args.name)
        if not machine_id:
            sys.exit("machine created but could not be found again")
        print(f"created machine {args.name} ({machine_id})")

    secret = asyncio.run(push_config_and_secret(machine_id, part_id, args.config))
    port = args.port or find_free_port()
    config_path = write_server_config(part_id, secret, args.workdir, port)

    if not args.no_start:
        # The same lock viam-server-ctl.sh takes, so this and the session's own start
        # cannot both find nothing running and both launch.
        lock_dir = os.environ.get("PERSIST_STATE_DIR", "/tmp/persist")
        os.makedirs(lock_dir, exist_ok=True)
        with open(os.path.join(lock_dir, "viam-server.lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            already = running_servers(config_path)
            if must_restart(already, old_part, part_id):
                print(term.yellow(
                    "viam-server was running for another machine's part "
                    f"({old_part}); restarting it for {args.name}."))
                stop_servers(already)
                already = []
            if already:
                print(term.yellow(
                    "viam-server is already running for this machine (pid "
                    f"{', '.join(map(str, already))}); not starting a second copy."))
                print("To restart it with this configuration: course-tools start-server --restart")
            else:
                start_server(find_viam_server(args.viam_server), config_path,
                             os.path.join(args.workdir, "viam-server.log"))

    write_env({"VIAM_102_ORG_ID": org_id, "VIAM_102_PART_ID": part_id,
               "VIAM_102_MACHINE_ID": machine_id,
               "VIAM_102_MACHINE_NAME": args.name})

    print()
    print(f"machine   : {args.name}")
    print(f"{term.bold('open it')}   : "
          f"{term.cyan(f'https://app.viam.com/machine/{machine_id}/configure')}")
    print(f"3d scene  : "
          f"{term.cyan(f'https://app.viam.com/machine/{machine_id}/3d-scene')}")
    print(f"part id   : {part_id}")
    print()
    print(term.bold("Open it in the Viam app") + " with the link above. Its")
    print("CONFIGURE tab should list arm-1, gripper-1, floor, pallet,")
    print("pick-station, workcell-scene and pack-sequencer, and the status")
    print("should read ONLINE.")
    print()
    print("Those are saved to ~/.viam-102-env. To see them again later, run:")
    print()
    print("    course-tools print-all")


def write_env(values):
    """Save the machine's values the course's commands need, and source them.

    Every page that reloads the module needs the part id, and a page that asks
    a learner to paste a UUID into a command is a page that gets it wrong.
    Worse, an unreplaced <your-part-id> is a shell redirect, so the error names
    a missing file rather than the placeholder. These become variables instead.

    The file is shared: check-namespace and check-names save their answers to
    it too, and the course tells learners to run this again when something is
    missing. So the keys given here are replaced and every other line is kept;
    rewriting the whole file would silently drop the namespace and module name.
    """
    path = os.path.expanduser("~/.viam-102-env")
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    lines = [ln for ln in lines
             if not any(ln.startswith(f"export {k}=") for k in values)]
    lines += [f"export {k}={shlex.quote(str(v))}" for k, v in values.items()]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")

    # New terminals pick them up; this one is told to source it.
    bashrc = os.path.expanduser("~/.bashrc")
    line = "source ~/.viam-102-env"
    try:
        existing = open(bashrc).read() if os.path.exists(bashrc) else ""
        if line not in existing:
            with open(bashrc, "a") as f:
                f.write(f"\n{line}\n")
    except OSError:
        print(f"note: could not add {line} to ~/.bashrc; run it by hand")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
