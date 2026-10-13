from baileys_lite.foundation.node_runtime import JSObject

SEQ_MOD = 0x10000
HALF = 0x8000


def seqDelta(a, b):
    d = (b - a) & 0xffff
    return d - SEQ_MOD if d >= HALF else d


class AudioReorderBuffer:
    def __init__(self, options=None):
        opts = JSObject(options if options is not None else {})
        self.next = -1
        self.pending = {}
        self.window = 3 if opts.get('window') is None else opts.get('window')
        self.maxConcealRun = 5 if opts.get('maxConcealRun') is None else opts.get('maxConcealRun')
        self.resyncDistance = 250 if opts.get('resyncDistance') is None else opts.get('resyncDistance')
        self.stats = JSObject({'duplicates': 0, 'late': 0, 'concealed': 0, 'resyncs': 0, 'delivered': 0})

    def reset(self):
        self.next = -1
        self.pending.clear()

    def push(self, seq, payload):
        out = []
        if self.next < 0:
            self.next = seq
        d = seqDelta(self.next, seq)
        if d < 0:
            if d < -self.resyncDistance:
                self.resync(seq)
            else:
                self.stats['late'] += 1
                return out
        elif d > self.resyncDistance:
            self.resync(seq)
        if seq in self.pending:
            self.stats['duplicates'] += 1
            return out
        self.pending[seq] = payload
        self.drain(out, False)
        return out

    def flush(self):
        out = []
        self.drain(out, True)
        return out

    def resync(self, seq):
        self.stats['resyncs'] += 1
        self.pending.clear()
        self.next = seq

    def drain(self, out, force):
        while True:
            if self.next in self.pending:
                out.append(self.pending[self.next])
                del self.pending[self.next]
                self.next = (self.next + 1) & 0xffff
                self.stats['delivered'] += 1
                continue
            if len(self.pending) == 0:
                return
            farthest = self.farthestAhead()
            if not force and farthest < self.window:
                return
            hole = min(self.maxConcealRun, self.holeLength())
            for i in range(hole):
                out.append(None)
                self.stats['concealed'] += 1
            self.next = (self.next + self.holeLength()) & 0xffff

    def farthestAhead(self):
        best = 0
        for s in self.pending.keys():
            d = seqDelta(self.next, s)
            if d > best:
                best = d
        return best

    def holeLength(self):
        best = None
        for s in self.pending.keys():
            d = seqDelta(self.next, s)
            if d >= 0 and (best is None or d < best):
                best = d
        return 0 if best is None else best
