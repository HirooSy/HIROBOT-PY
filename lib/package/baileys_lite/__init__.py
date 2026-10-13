from baileys_lite.foundation.node_runtime import JSObject
from baileys_lite.defaults import DEFAULT_CONNECTION_CONFIG
from baileys_lite.socket.business_communities import makeCommunitiesSocket
from baileys_lite.WAProto import *
from baileys_lite.constants import *
from baileys_lite.defaults import *
from baileys_lite.binary.wa_binary import *
from baileys_lite.wam.wam import *
from baileys_lite.wam.wam_constants import *
from baileys_lite.socket.usync import *
from baileys_lite.foundation.ai_rich import *
from baileys_lite.foundation.qrcode_terminal import *
from baileys_lite.utils.wa_protocol_core import *
from baileys_lite.utils.auth_state_core import *
from baileys_lite.utils.auth_state_storage import *
from baileys_lite.utils.media import *
from baileys_lite.utils.message_compose import *
from baileys_lite.utils.chat_sync import *
from baileys_lite.utils.message_processing import *


def makeWASocket(config=None):
    cfg = config if config is not None else JSObject({})
    newConfig = JSObject({**DEFAULT_CONNECTION_CONFIG, **cfg})
    newConfig['logger'] = cfg.get('logger') if cfg.get('logger') is not None else DEFAULT_CONNECTION_CONFIG['logger']
    return makeCommunitiesSocket(newConfig)


default = makeWASocket
