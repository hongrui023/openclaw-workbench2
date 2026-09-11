判断下面这条生活记录属于哪一类，并且**只输出一个 JSON 对象**。

当前时间：{{current_time}}

## 分类标准

- `expense`：收支记账。句中有金额，且涉及花钱、收入、报销、退款、缴费、转账等。
- `todo`：待办事项。将来要做的事，或需要记住的提醒。
- `note`：随笔 / 想法。以上都不是的个人记录。

## 输出格式

只输出 JSON，不要任何解释文字，不要代码块标记：

{"type":"expense 或 todo 或 note","amount":数字或 null,"direction":"in 或 out 或 null","summary":"一句话摘要"}

字段要求：

- `amount`：`type` 为 `expense` 时必须是数字（单位：元），不要带单位符号；其他类型为 `null`。
- `direction`：`out` 表示支出，`in` 表示收入；非 expense 时为 `null`。
- `summary`：这条记录的简短概括，不超过 30 字。
  - expense 写成「花在哪 / 买了什么」，例如「买实验耗材」；
  - todo 保留完整事项；
  - note 用原意提炼。

【待分类内容】
{{content}}
