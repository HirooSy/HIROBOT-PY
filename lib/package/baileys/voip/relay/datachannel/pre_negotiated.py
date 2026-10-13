from baileys.foundation.node_runtime import EventEmitter
from baileys.voip.relay.sctp.association import SctpAssociation


class PreNegotiatedDataChannel(EventEmitter):
    def __init__(self, sendDtlsPayload=None, logger=None):
        super().__init__()
        self.association = SctpAssociation(sendDtlsPayload=sendDtlsPayload, logger=logger)
        self.association.on('connected', lambda: self.emit('open'))
        self.association.on('message', lambda payload: self.emit('message', payload))
        self.association.on('error', lambda err: self.emit('error', err))

    def start(self):
        self.association.start()

    def close(self):
        self.association.close()

    def getSendBacklog(self):
        return self.association.getSendBacklog()

    @property
    def readyState(self):
        return 'open' if self.association.isConnected else 'connecting'

    def handleDtlsPayload(self, plaintext):
        self.association.handleDtlsPayload(plaintext)

    def send(self, payload, isAudio=True):
        self.association.send(bytes(payload), isAudio)
