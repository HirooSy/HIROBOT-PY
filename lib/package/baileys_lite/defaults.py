from baileys_lite.foundation.logger import createLogger
from baileys_lite.foundation.node_runtime import JSObject
from baileys_lite.signal.signal_repository import makeLibSignalRepository
from baileys_lite.utils.wa_protocol_core import Browsers, KEY_BUNDLE_TYPE
from baileys_lite.WAProto import proto

logger = createLogger()

version = [2, 3000, 1047970367]


async def asyncUndefined(*args):
    return None


UNAUTHORIZED_CODES = [401, 403, 419]
BIZ_BOT_SUPPORT_PAYLOAD = '{"version":1,"is_ai_message":true,"should_upload_client_logs":false,"should_show_system_message":false,"ticket_id":"7004947587700716","citation_items":[],"ticket_locale":"us"}'
DEFAULT_ORIGIN = 'https://web.whatsapp.com'
CALL_VIDEO_PREFIX = 'https://call.whatsapp.com/video/'
CALL_AUDIO_PREFIX = 'https://call.whatsapp.com/voice/'
LIBRARY_NAME = 'Baileys'
DEF_CALLBACK_PREFIX = 'CB:'
DEF_TAG_PREFIX = 'TAG:'
PHONE_CONNECTION_CB = 'CB:Pong'
WA_ADV_ACCOUNT_SIG_PREFIX = bytes([6, 0])
WA_ADV_DEVICE_SIG_PREFIX = bytes([6, 1])
WA_ADV_HOSTED_ACCOUNT_SIG_PREFIX = bytes([6, 5])
WA_ADV_HOSTED_DEVICE_SIG_PREFIX = bytes([6, 6])
WA_DEFAULT_EPHEMERAL = 7 * 24 * 60 * 60
STATUS_EXPIRY_SECONDS = 24 * 60 * 60
PLACEHOLDER_MAX_AGE_SECONDS = 14 * 24 * 60 * 60
NOISE_MODE = 'Noise_XX_25519_AESGCM_SHA256\x00\x00\x00\x00'
DICT_VERSION = 3

KEY_BUNDLE_TYPE = KEY_BUNDLE_TYPE
NOISE_WA_HEADER = bytes([87, 65, 6, DICT_VERSION])

WA_CERT_DETAILS = JSObject({
    'SERIAL': 0,
    'ISSUER': 'WhatsAppLongTerm1',
    'PUBLIC_KEY': bytes.fromhex('142375574d0a587166aae71ebe516437c4a28b73e3695c6ce1f7f9545da8ee6b')
})

PROCESSABLE_HISTORY_TYPES = [
    proto.HistorySync.HistorySyncType.INITIAL_BOOTSTRAP,
    proto.HistorySync.HistorySyncType.PUSH_NAME,
    proto.HistorySync.HistorySyncType.RECENT,
    proto.HistorySync.HistorySyncType.FULL,
    proto.HistorySync.HistorySyncType.ON_DEMAND,
    proto.HistorySync.HistorySyncType.NON_BLOCKING_DATA,
    proto.HistorySync.HistorySyncType.INITIAL_STATUS_V3
]

DEFAULT_CACHE_TTLS = JSObject({
    'SIGNAL_STORE': 5 * 60,
    'MSG_RETRY': 60 * 60,
    'CALL_OFFER': 5 * 60,
    'USER_DEVICES': 5 * 60
})

DEFAULT_CONNECTION_CONFIG = JSObject({
    'version': version,
    'browser': Browsers['macOS']('Chrome'),
    'waWebSocketUrl': 'wss://web.whatsapp.com/ws/chat',
    'connectTimeoutMs': 20000,
    'keepAliveIntervalMs': 30000,
    'logger': logger.child(JSObject({'class': 'baileys'})),
    'emitOwnEvents': True,
    'defaultQueryTimeoutMs': 60000,
    'customUploadHosts': [],
    'retryRequestDelayMs': 250,
    'maxMsgRetryCount': 5,
    'fireInitQueries': True,
    'disableStickyRouting': False,
    'auth': None,
    'markOnlineOnConnect': True,
    'syncFullHistory': True,
    'patchMessageBeforeSending': lambda msg: msg,
    'shouldSyncHistoryMessage': lambda opts: opts.get('syncType') != proto.HistorySync.HistorySyncType.FULL,
    'shouldIgnoreJid': lambda *args: False,
    'linkPreviewImageThumbnailWidth': 192,
    'transactionOpts': JSObject({'maxCommitRetries': 10, 'delayBetweenTriesMs': 3000}),
    'generateHighQualityLinkPreview': False,
    'enableAutoSessionRecreation': True,
    'enableRecentMessageCache': True,
    'options': JSObject({}),
    'appStateMacVerification': JSObject({'patch': False, 'snapshot': False}),
    'countryCode': 'US',
    'getMessage': asyncUndefined,
    'cachedGroupMetadata': asyncUndefined,
    'makeSignalRepository': makeLibSignalRepository
})

HISTORY_SYNC_PAUSED_TIMEOUT_MS = 120000
MIN_PREKEY_COUNT = 5
INITIAL_PREKEY_COUNT = 812
UPLOAD_TIMEOUT = 30000

TimeMs = JSObject({
    'Minute': 60 * 1000,
    'Hour': 60 * 60 * 1000,
    'Day': 24 * 60 * 60 * 1000,
    'Week': 7 * 24 * 60 * 60 * 1000
})
