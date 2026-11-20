"""Wire-format guidance, independent of semantic extraction rules/checkpoints."""
from .models import CompactExtractionBatch, ExtractionBatch, ResolutionDecision


_COMMON = """输出格式检查（提交前逐项核对）：
只调用一次指定工具，将一个完整 JSON 对象作为工具参数；不输出解释、Markdown 或 JSON 字符串。
只使用工具 Schema 声明的字段与枚举值，字段名和枚举值使用英文原值。
entities、relations、identity_links 都是数组；没有候选的数组用 []，不能用 null、字符串或对象。
实体 local_id 在本批唯一；source、target 必须逐字引用本批已列出的 local_id，不能填实体名称。
实体 type 只能是 person/place/organization/item/skill/concept；polarity 只能是 affirmed/denied；
statement_mode 只能是 narration/character_claim。occurrence 为从 1 开始的整数，不能填字符串。
不能确定的候选不要输出；不要用缺字段、空证据或自造枚举值表示不确定。
空结果也必须调用工具，参数为 {"entities":[],"relations":[],"identity_links":[]}。
下面示例仅说明参数形状，人物、编号及事实不得复制到实际结果。"""

_FULL = """调用 ExtractionBatch。实体使用 mentions；关系和身份线索使用 evidence。
mentions/evidence 是锚点对象数组；每个锚点只含 span_id、quote、occurrence。
span_id 只能取输入已有的片段编号；实体 mentions.quote 等于 name；name 长度为 1–100 字。
description 可省略，填写时最多 300 字。关系及身份线索 evidence 必须有 1–3 个锚点，quote 为 1–600 字原文。
不能使用精简格式的 evidence_ids，不能把实体的 span_id/occurrence 放在实体顶层。
假设输入 core 是“张三拜李四为师。张三又名阿三。”，参数示例：
{"entities":[{"local_id":"e1","name":"张三","type":"person","mentions":[{"span_id":"core","quote":"张三","occurrence":1}]},{"local_id":"e2","name":"李四","type":"person","mentions":[{"span_id":"core","quote":"李四","occurrence":1}]},{"local_id":"e3","name":"阿三","type":"person","mentions":[{"span_id":"core","quote":"阿三","occurrence":1}]}],"relations":[{"source":"e1","target":"e2","type":"disciple_of","polarity":"affirmed","statement_mode":"narration","evidence":[{"span_id":"core","quote":"张三拜李四为师。","occurrence":1}]}],"identity_links":[{"source":"e1","target":"e3","evidence":[{"span_id":"core","quote":"张三又名阿三。","occurrence":1}]}]}"""

_COMPACT = """调用 CompactExtractionBatch。实体只含 local_id、name、type、span_id、occurrence。
span_id 是输入 segments 中实际存在的 id，不是 region 值 core/before/after；name 长度为 1–100 字。
关系只含 source、target、type、evidence_ids、polarity、statement_mode。
身份线索只含 source、target、evidence_ids。evidence_ids 为 1–3 个输入片段 id 的字符串数组。
不得添加 mentions、description、quote 或 evidence；不能在 evidence_ids 中放原文或对象。
假设输入 s0（core）是“张三拜李四为师。张三又名阿三。”，参数示例：
{"entities":[{"local_id":"e1","name":"张三","type":"person","span_id":"s0","occurrence":1},{"local_id":"e2","name":"李四","type":"person","span_id":"s0","occurrence":1},{"local_id":"e3","name":"阿三","type":"person","span_id":"s0","occurrence":1}],"relations":[{"source":"e1","target":"e2","type":"disciple_of","evidence_ids":["s0"],"polarity":"affirmed","statement_mode":"narration"}],"identity_links":[{"source":"e1","target":"e3","evidence_ids":["s0"]}]}"""


def output_contract(schema: type) -> str:
    if schema is ExtractionBatch:
        return _COMMON + "\n" + _FULL
    if schema is CompactExtractionBatch:
        return _COMMON + "\n" + _COMPACT
    if schema is ResolutionDecision:
        return """只调用一次 ResolutionDecision 工具，参数为一个完整 JSON 对象，不添加其他字段或解释。
decision 只能为 merge/new/ambiguous。merge 时 candidate_id 引用输入候选编号，
supporting_mentions 为输入 mention_id 的非空字符串数组；new/ambiguous 使用 candidate_id=null。
有候选时 new 的 supporting_mentions 必须引用区分同名异人的原文证据；无候选的 new 和 ambiguous 使用 supporting_mentions=[]。
没有充分身份依据时参数示例：{"decision":"ambiguous","candidate_id":null,"supporting_mentions":[]}。"""
    return ""
