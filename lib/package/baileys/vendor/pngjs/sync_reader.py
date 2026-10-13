from baileys.foundation.node_runtime import JSError, JSObject


class SyncReader:
    def __init__(self, buffer):
        self._buffer = bytes(buffer)
        self._offset = 0
        self._reads = []

    def _remaining(self):
        return len(self._buffer) - self._offset

    def read(self, length, callback):
        self._reads.append(JSObject({
            'length': abs(length),
            'allowLess': length < 0,
            'func': callback,
        }))

    def process(self):
        while len(self._reads) > 0 and self._remaining() > 0:
            read = self._reads[0]

            if self._remaining() > 0 and (self._remaining() >= read.get('length') or read.get('allowLess')):
                self._reads.pop(0)

                start = self._offset
                chunk = self._buffer[start:start + read.get('length')]
                self._offset = min(start + read.get('length'), len(self._buffer))

                read.get('func')(chunk)
            else:
                break

        if len(self._reads) > 0:
            raise JSError("There are some read requests waitng on finished stream")

        if self._remaining() > 0:
            raise JSError("unrecognised content at end of stream")


default = SyncReader
