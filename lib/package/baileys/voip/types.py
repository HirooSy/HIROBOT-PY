from baileys.foundation.node_runtime import JSObject


class CallState:
    Initiating = 'initiating'
    Ringing = 'ringing'
    IncomingRinging = 'incoming_ringing'
    Connecting = 'connecting'
    Active = 'active'
    OnHold = 'on_hold'
    Ended = 'ended'


class CallDirection:
    Outgoing = 'outgoing'
    Incoming = 'incoming'


class CallMediaType:
    Audio = 'audio'
    Video = 'video'


class EndCallReason:
    UserEnded = 'user_ended'
    Declined = 'declined'
    Timeout = 'timeout'
    Busy = 'busy'
    Cancelled = 'cancelled'
    Failed = 'failed'
    DoNotDisturb = 'do_not_disturb'
    Unknown = 'unknown'


class PayloadType:
    WhatsAppOpus = 120
    H264 = 97


DEFAULT_VIDEO_CONFIG = JSObject({
    'width': 640,
    'height': 360,
    'frameRate': 20,
    'clockRate': 90000,
})

DEFAULT_AUDIO_CONFIG = JSObject({
    'sampleRate': 16000,
    'captureChunkSize': 960,
    'playbackOutputSize': 256,
    'maxBufferSize': 1600,
    'intervalMs': 60,
})

SRTP_SEND_AUTH_TAG_LEN = 4
SRTP_RECV_AUTH_TAG_LEN = 4
SRTP_AUTH_TAG_LEN = 4

SRTP_LABEL = JSObject({
    'ENCRYPTION': 0x00,
    'AUTH': 0x01,
    'SALT': 0x02,
})

SRTCP_LABEL = JSObject({
    'ENCRYPTION': 0x03,
    'AUTH': 0x04,
    'SALT': 0x05,
})

RTCP_AUTH_TAG_LEN = 10
WA_RELAY_PORT = 3480
WA_DTLS_FINGERPRINT = 'sha-256 F9:CA:0C:98:A3:CC:71:D6:42:CE:5A:E2:53:D2:15:20:D3:1B:BA:D8:57:A4:F0:AF:BE:0B:FB:F3:6B:0C:A0:68'
