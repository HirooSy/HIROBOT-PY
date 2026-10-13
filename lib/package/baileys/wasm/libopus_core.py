import ctypes
import os

from baileys.foundation.node_runtime import JSError
from baileys.wasm.deps import ensureWasmtime

PAGE = 65536
M32 = 0xFFFFFFFF
I32_MIN = -0x80000000


def toI32(value):
    if isinstance(value, int):
        value &= M32
        return value - 0x100000000 if value > 0x7FFFFFFF else value
    return value


class LibopusCore:
    def __init__(self, path, exportNames, initExport, memoryExport):
        if not os.path.isfile(path):
            raise JSError(f'libopus WASM core missing: {path}')
        wt = ensureWasmtime()
        self.wt = wt
        self.exportNames = exportNames
        self.engine = wt.Engine()
        self.store = wt.Store(self.engine)
        with open(path, 'rb') as handle:
            moduleBytes = handle.read()
        module = wt.Module(self.engine, moduleBytes)
        linker = wt.Linker(self.engine)
        self.defineHostFunctions(linker)
        self.instance = linker.instantiate(self.store, module)
        self.exports = self.instance.exports(self.store)
        self.memory = self.exports[memoryExport]
        self.call(initExport)

    def defineHostFunctions(self, linker):
        wt = self.wt
        i32 = wt.ValType.i32()
        f64 = wt.ValType.f64()

        def define(name, params, results, callback):
            linker.define_func('a', name, wt.FuncType(params, results), callback)

        define('a', [i32, f64], [i32], lambda which, timeoutMs: 0)
        define('b', [i32], [i32], self.resizeHeap)
        define('c', [i32], [], self.procExit)
        define('d', [], [], lambda: None)
        define('e', [i32, i32, i32, i32], [i32], self.fdWrite)
        define('f', [], [], self.abortJs)

    def resizeHeap(self, requestedSize):
        current = self.memory.data_len(self.store)
        if requestedSize <= current:
            return 1
        pages = (requestedSize - current + PAGE - 1) // PAGE
        try:
            self.memory.grow(self.store, pages)
            return 1
        except Exception:
            return 0

    def fdWrite(self, fd, iovs, iovsLen, nwrittenPtr):
        total = 0
        for index in range(iovsLen):
            total += int.from_bytes(self.memRead(iovs + index * 8 + 4, 4), 'little')
        self.memWrite(nwrittenPtr, total.to_bytes(4, 'little'))
        return 0

    def procExit(self, code):
        raise JSError(f'libopus WASM core exited with code {code}')

    def abortJs(self):
        raise JSError('libopus WASM core aborted')

    def baseAddress(self):
        return ctypes.cast(self.memory.data_ptr(self.store), ctypes.c_void_p).value

    def memSize(self):
        return self.memory.data_len(self.store)

    def memRead(self, address, length):
        if address < 0 or address + length > self.memSize():
            raise JSError('libopus WASM memory read out of bounds')
        return ctypes.string_at(self.baseAddress() + address, length)

    def memWrite(self, address, data):
        if address < 0 or address + len(data) > self.memSize():
            raise JSError('libopus WASM memory write out of bounds')
        ctypes.memmove(self.baseAddress() + address, bytes(data), len(data))

    def call(self, name, *args):
        export = self.exports[self.exportNames.get(name, name)]
        converted = [toI32(arg) for arg in args]
        try:
            return export(self.store, *converted)
        except self.wt.Trap as err:
            raise JSError(f'libopus WASM core trap: {err.message if hasattr(err, "message") else err}')
