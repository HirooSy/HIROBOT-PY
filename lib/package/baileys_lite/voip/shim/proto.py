from baileys_lite.voip.shim.baileys_resolve import resolveBaileysModule


cachedProto = None


async def getProto():
    global cachedProto
    if cachedProto is not None:
        return cachedProto
    module = await resolveBaileysModule()
    cachedProto = module.proto
    return cachedProto
