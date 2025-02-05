import logging

logger = logging.getLogger(__name__)


class WyzeKvs:
    def __init__(debug: bool = False):
        if debug:
            logging.basicConfig()
            logger.setLevel(logging.DEBUG)
