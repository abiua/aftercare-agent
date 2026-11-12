"""Writing instructions for generated prose; structured business contracts stay unchanged."""

LANGUAGE_VERSION = "plain-zh-v1"
PLAIN_LANGUAGE = (
    "表达规则plain-zh-v1：自然语言写给客户或客服看，先直接回答，再写具体依据和下一步。"
    "notes、rationale、unresolved、missing_evidence、alternatives及判断说明都用简短日常中文。"
    "正文用商品名称或第一件/第二件商品，金额用元；内部ID、英文状态、政策编号和版本"
    "只放在专用结构字段。结构字段的ID、引用、版本保持精确，amount_cents仍用整数分；"
    "引用原文保持原样。不要把内部约束抄成免责声明，不写‘仅作为待审核的处置候选’、"
    "‘不构成批准或执行’、‘材料审核APPROVED不等于退款获批’等套话。"
    "建议就写‘建议退款…’，真实待办写‘请客服核对…’或‘等待财务审批’，同一事项只说一次。"
    "只列实际缺口和冲突，不追加假想风险。没查到退款记录就答‘目前没查到这笔订单的退款处理记录。’"
    "不再追加‘不能确定是否申请过退款’。未查过则写‘还没核对…’，不要写成没有记录。"
    "疑似、客户陈述、人工检查结果及实际审批/执行状态须如实区分；简洁不能改动事实。"
    "客户无法补图时给出可行替代或转人工，不反复催图，不要求客户提供内部材料ID或哈希。"
)
