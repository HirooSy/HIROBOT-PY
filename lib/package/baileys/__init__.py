from baileys.foundation.node_runtime import JSObject
from baileys.defaults import DEFAULT_CONNECTION_CONFIG
from baileys.socket.business_communities import makeCommunitiesSocket
from baileys.WAProto import *
from baileys.constants import *
from baileys.defaults import *
from baileys.binary.wa_binary import *
from baileys.wam.wam import *
from baileys.wam.wam_constants import *
from baileys.socket.usync import *
from baileys.foundation.ai_rich import *
from baileys.foundation.qrcode_terminal import *
from baileys.utils.wa_protocol_core import *
from baileys.utils.auth_state_core import *
from baileys.utils.auth_state_storage import *
from baileys.utils.media import *
from baileys.utils.message_compose import *
from baileys.utils.chat_sync import *
from baileys.utils.message_processing import *


def makeWASocket(config=None):
    cfg = config if config is not None else JSObject({})
    newConfig = JSObject({**DEFAULT_CONNECTION_CONFIG, **cfg})
    newConfig['logger'] = cfg.get('logger') if cfg.get('logger') is not None else DEFAULT_CONNECTION_CONFIG['logger']
    return makeCommunitiesSocket(newConfig)


default = makeWASocket
