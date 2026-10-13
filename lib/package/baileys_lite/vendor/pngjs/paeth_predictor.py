def paethPredictor(left, above, upLeft):
    paeth = left + above - upLeft
    pLeft = abs(paeth - left)
    pAbove = abs(paeth - above)
    pUpLeft = abs(paeth - upLeft)

    if pLeft <= pAbove and pLeft <= pUpLeft:
        return left
    if pAbove <= pUpLeft:
        return above
    return upLeft


default = paethPredictor
