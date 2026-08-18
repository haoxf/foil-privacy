# Multi-Agent Brief

委派子 Agent 前填写。指针优先：给路径、commit、行号与验收指针；不要粘贴完整会话、完整大 diff、大日志或整个结果包。  
**有本模板不等于必须额外启动子 Agent**；是否委派按 `.cursor/rules/nr-agent-workflow.mdc`（要求派发时必须派发），简报与执行细则按 `.cursor/rules/nr-agent-delegation.mdc`。

安装路径：`.engineering-agent-rules/templates/agent-session-kits/multi-agent-brief.md`。项目私有注记可另写 overlay，不要改本托管文件。

## Fields

| Field | Content |
| --- | --- |
| Role | `explorer` \| `implementer` \| `root-cause` \| `reviewer` \| `verify-only`（默认只选一个） |
| Goal | 一个边界明确的问题或交付 |
| Context pointers | 路径 / commit 或 diff 范围 / 规格或验收指针 / 证据位置 |
| Allowed | 允许的读路径、写路径或动作 |
| Ownership | 本 Agent 独占写入范围；只读角色填 `n/a` |
| Forbidden | 例如：扩 scope、再委派、push/merge、贴大日志、改用户 worktree |
| Output | 见下方角色契约；勿复述任务全文 |
| Stop | 完成条件；证据不足或越权时停止 |

小任务可合并简写 Allowed/Ownership/Forbidden，但 Goal、Context pointers、Output、Stop 不得空。

## Role output contracts

真源仍是共享 `nr-agent-workflow` / `nr-agent-delegation` / `nr-delivery-gate`；此处仅便于粘贴。

| Role | Output |
| --- | --- |
| `reviewer` | findings、证据、未确认项和结论 |
| `root-cause` | 候选根因或「证据不足」、支持/反驳证据指针、反假设、一个下一步验证、未确认项 |
| `implementer` | 改动摘要、路径列表、如何验证、未做事项 |
| `explorer` | 结论、关键路径指针、未确认项 |
| `verify-only` | 已跑什么、通过/失败、**未跑什么及原因** |

## Filled example skeleton

```text
Role: reviewer
Goal: 审查冻结候选是否满足验收且无阻塞缺陷，并覆盖交付门禁最低审查面
Context pointers: commit <sha>；files: a.swift, aTests.swift；验收: …
Allowed: 只读仓库；读取冻结 diff 与失败摘要路径
Ownership: n/a
Forbidden: 修改代码；继续委派；复述任务；只根据主 Agent 摘要下结论
Output: findings、证据、未确认项和结论
Stop: 列出阻塞与非阻塞项后结束；候选文件若已变则停止并报告失效
```
