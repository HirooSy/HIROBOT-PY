import re

USERNAME_MIN_LENGTH = 4
USERNAME_MAX_LENGTH = 30

USERNAME_PATTERN = re.compile(r'[a-zA-Z][a-zA-Z0-9._]*')
USERNAME_KEY_PATTERN = re.compile(r'[0-9]{4}')


def normalizeUsername(input_):
    if not isinstance(input_, str):
        return ''
    return re.sub(r'^@', '', input_.strip(), count=1)


def isValidUsername(input_):
    handle = normalizeUsername(input_)
    if len(handle) < USERNAME_MIN_LENGTH or len(handle) > USERNAME_MAX_LENGTH:
        return False
    return USERNAME_PATTERN.fullmatch(handle) is not None


def isValidUsernameKey(key):
    return isinstance(key, str) and USERNAME_KEY_PATTERN.fullmatch(key) is not None
