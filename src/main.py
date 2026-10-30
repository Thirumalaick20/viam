import asyncio
from viam.module.module import Module
from models.palletizer import Palletizer as PalletizerModel


if __name__ == '__main__':
    asyncio.run(Module.run_from_registry())
