import math

from baileys.foundation.node_runtime import JSObject
from baileys.voip.bytes import writeUInt16BE, writeUInt32BE
from baileys.voip.crypto.primitives import randomBytes

RTCP_PT = JSObject({
    'SR': 200,
    'SDES': 202
})

NTP_UNIX_OFFSET_SECS = 2208988800
WHATSAPP_RTCP_CNAME_LEN = 18

HEX_CHARS = '0123456789abcdef'


def buildWhatsappRtcpCname(entropy):
    randomHex = []
    for nibble in range(11):
        b = entropy[6 + nibble // 2]
        randomHex.append(HEX_CHARS[b >> 4] if (nibble & 1) == 0 else HEX_CHARS[b & 0x0f])
    cname = bytearray(WHATSAPP_RTCP_CNAME_LEN)
    first = ''.join(randomHex[0:5]).encode('utf-8')
    cname[0:len(first)] = first
    cname[5:8] = '@pj'.encode('utf-8')
    rest = ''.join(randomHex[5:]).encode('utf-8')
    cname[8:8 + len(rest)] = rest
    cname[14:18] = '.org'.encode('utf-8')
    return bytes(cname)


def buildSenderReport(localSsrc, stats, nowMs):
    buf = bytearray(28)
    buf[0] = 0x80
    buf[1] = RTCP_PT['SR']
    buf[3] = 6
    writeUInt32BE(buf, localSsrc, 4)
    ntpSec = math.floor(nowMs / 1000) + NTP_UNIX_OFFSET_SECS
    ntpFrac = math.floor(((nowMs % 1000) / 1000) * 4294967296 + 0.5)
    writeUInt32BE(buf, ntpSec & 0xFFFFFFFF, 8)
    writeUInt32BE(buf, ntpFrac & 0xFFFFFFFF, 12)
    writeUInt32BE(buf, stats.get('rtpTimestamp') & 0xFFFFFFFF, 16)
    writeUInt32BE(buf, stats.get('packetsSent') & 0xFFFFFFFF, 20)
    writeUInt32BE(buf, stats.get('octetsSent') & 0xFFFFFFFF, 24)
    return bytes(buf)


def buildSourceDescription(localSsrc, cname):
    packet = bytearray(32)
    packet[0] = 0x81
    packet[1] = RTCP_PT['SDES']
    writeUInt16BE(packet, 7, 2)
    writeUInt32BE(packet, localSsrc, 4)
    packet[8] = 1
    packet[9] = WHATSAPP_RTCP_CNAME_LEN
    packet[10:10 + len(cname)] = cname
    return bytes(packet)


def buildSenderReportWithSdes(localSsrc, stats, nowMs, cname):
    sr = buildSenderReport(localSsrc, stats, nowMs)
    sdes = buildSourceDescription(localSsrc, cname)
    return sr + sdes


def generateWhatsappRtcpCname():
    return buildWhatsappRtcpCname(randomBytes(12))
