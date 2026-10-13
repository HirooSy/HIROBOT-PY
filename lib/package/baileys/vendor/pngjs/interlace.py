from baileys.foundation.node_runtime import JSObject


imagePasses = [
    JSObject({
        'x': [0],
        'y': [0],
    }),
    JSObject({
        'x': [4],
        'y': [0],
    }),
    JSObject({
        'x': [0, 4],
        'y': [4],
    }),
    JSObject({
        'x': [2, 6],
        'y': [0, 4],
    }),
    JSObject({
        'x': [0, 2, 4, 6],
        'y': [2, 6],
    }),
    JSObject({
        'x': [1, 3, 5, 7],
        'y': [0, 2, 4, 6],
    }),
    JSObject({
        'x': [0, 1, 2, 3, 4, 5, 6, 7],
        'y': [1, 3, 5, 7],
    }),
]


def getImagePasses(width, height):
    images = []
    xLeftOver = width % 8
    yLeftOver = height % 8
    xRepeats = (width - xLeftOver) // 8
    yRepeats = (height - yLeftOver) // 8
    for i in range(len(imagePasses)):
        pass_ = imagePasses[i]
        passWidth = xRepeats * len(pass_.get('x'))
        passHeight = yRepeats * len(pass_.get('y'))
        for j in range(len(pass_.get('x'))):
            if pass_.get('x')[j] < xLeftOver:
                passWidth += 1
            else:
                break
        for j in range(len(pass_.get('y'))):
            if pass_.get('y')[j] < yLeftOver:
                passHeight += 1
            else:
                break
        if passWidth > 0 and passHeight > 0:
            images.append(JSObject({'width': passWidth, 'height': passHeight, 'index': i}))
    return images


def getInterlaceIterator(width):
    def iterator(x, y, pass_):
        xs = imagePasses[pass_].get('x')
        ys = imagePasses[pass_].get('y')
        outerXLeftOver = x % len(xs)
        outerX = ((x - outerXLeftOver) // len(xs)) * 8 + xs[outerXLeftOver]
        outerYLeftOver = y % len(ys)
        outerY = ((y - outerYLeftOver) // len(ys)) * 8 + ys[outerYLeftOver]
        return outerX * 4 + outerY * width * 4
    return iterator
