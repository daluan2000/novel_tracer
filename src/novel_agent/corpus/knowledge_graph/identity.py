"""Conservative, source-bound gates for identity proposals, not an identity oracle."""
from __future__ import annotations

import re
from functools import lru_cache


GENERIC_NAMES = {
    "他", "她", "它", "他们", "她们", "自己", "师父", "师傅", "陛下", "先生",
    "少年", "少女", "老人", "男子", "女子", "姑娘", "哥儿", "妹妹", "哥哥",
    "姐姐", "弟弟", "老爷", "太太", "夫人", "公子", "小姐", "那人", "众人",
}

_SEP = r"[\s，,：:‘’“”\"《》]*"
_MARKER = (r"(?:(?:的)?(?:全名|原名|本名|曾用名|别名|化名|乳名|笔名|简称|别号)(?:是|为|叫作|叫)?"
           r"|又名|又叫|又称|亦称|也叫|也称|改名为|改名叫|更名为|更名|字|号|自号|人称|被称作|被称为)")
_NEGATIVE = re.compile(r"并非|不是|并不是|并不叫|不叫|并非同|不同的人|不同人|假冒|冒充|自称|据说|传闻|听说|误以为|误称|并非本名")


def explicit_identity(left: str, right: str, evidence: list[dict]) -> bool:
    """Require a positive predicate binding these exact endpoints in one clause.

    Co-occurrence, answering another person's name, and indirect address are
    deliberately insufficient. The semantic model must still confirm a match.
    """
    if left == right or left in GENERIC_NAMES or right in GENERIC_NAMES:
        return False
    for span in evidence:
        for sentence in re.split(r"[。！？!?；;\n]", span["quote"]):
            if _NEGATIVE.search(sentence):
                continue
            for a, b in ((left, right), (right, left)):
                a, b = re.escape(a), re.escape(b)
                patterns = (
                    a + _SEP + _MARKER + _SEP + b + r"(?![\u4e00-\u9fff])",
                    a + _SEP + r"(?:就是|正是|即是|即|实为|原是|原来是)" + _SEP + b + r"(?:本人|本尊)?" + _SEP + r"$",
                    a + _SEP + r"(?:和|与|同)" + _SEP + b + _SEP + r"(?:是|实为|原是)(?:同一人|同一个人|同一位人物)",
                    a + _SEP + r"(?:是|为|实为|正是)" + _SEP + b + r"(?:给自己取)?的(?:全名|原名|本名|曾用名|别名|化名|乳名|笔名|简称|别号)" + _SEP + r"$",
                )
                if any(re.search(pattern, sentence) for pattern in patterns):
                    return True
    return False


@lru_cache(maxsize=128)
def _distinct_patterns(names: tuple[str, ...]):
    alternatives = "|".join(re.escape(name) for name in names)
    a, b = f"(?P<left>{alternatives})", f"(?P<right>{alternatives})"
    return tuple(re.compile(pattern) for pattern in (
        a + _SEP + r"(?:和|与|同|、)" + _SEP + b + r"[^。！？!?；;\n]{0,12}(?:二人|两人|不是同一|并非同一|在一处|同行|同坐)",
        a + _SEP + r"(?:因让|让|向|对|问|告诉|询问|拜访)" + _SEP + b,
        a + _SEP + r"(?:不是|并非)" + _SEP + b + r"(?:本人)?(?=[\s。！？!?；;，,‘’“”\"：:]|$)",
    ))


def distinct_pairs(names, quotes):
    """Scan each context once rather than compiling a regex per possible pair."""
    names = tuple(sorted(set(names), key=lambda name: (-len(name), name)))
    if len(names) < 2:
        return set()
    pairs = set()
    for quote in quotes:
        for pattern in _distinct_patterns(names):
            for match in pattern.finditer(quote):
                left, right = match.group("left", "right")
                if left != right:
                    pairs.add((left, right))
    return pairs


def distinct_identity(left: str, right: str, quote: str) -> bool:
    return bool(distinct_pairs((left, right), [quote]))
