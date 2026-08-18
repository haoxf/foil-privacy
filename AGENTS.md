# 项目 Agent 指令

<!-- rulesctl:managed:start -->
## 共享工作流规则

基线由 `.cursor/rules/` 下已安装的 `nr-*.mdc` 提供。Cursor 按 frontmatter / globs 路由；Codex 等非 Cursor Agent 须按下方索引打开匹配规则，勿依赖未列出的全文。始终生效规则无需场景触发。

已安装 pack：`agent-model-router`, `agent-session-kits`, `agent-task-runtime`, `codex-agent-team`, `host-agent-adapter`

始终生效：

- 统一模型路由——按能力、深度、评分与额度选择执行端 → `.cursor/rules/nr-agent-model-router-execution-trigger.mdc`
- 任务运行时路由——强化持久多票对齐与续接 → `.cursor/rules/nr-agent-task-runtime-execution-trigger.mdc`
- 共享 Agent 工作流——三路径、自主推进、授权与审查门槛 → `.cursor/rules/nr-agent-workflow.mdc`
- 共享编码纪律——先理解和前置真实探针，后做最小可验证改动 → `.cursor/rules/nr-coding-discipline.mdc`
- 共享 Git 安全规则——保持用户上下文，过闸任务改动默认本地提交 → `.cursor/rules/nr-git-workflow.mdc`
- Host adapter 路由——跨宿主派发冻结叶子；禁止自调 → `.cursor/rules/nr-host-agent-adapter-execution-trigger.mdc`
- 用户可见回复——先给结论；不叙述过程；细节按需展开 → `.cursor/rules/nr-user-facing-reply.mdc`

按需：

- 共享多 Agent 委派——简报预算、并行、审查/根因执行细则与降级 → `.cursor/rules/nr-agent-delegation.mdc`
- 模型路由协议——两层评分缓存、异步额度快照、显式能力门槛、cursor_session 派发与上层 Review 闭环 → `.cursor/rules/nr-agent-model-router-model-routing.mdc`
- 多 Agent 简报——委派前按已安装 multi-agent-brief 模板填有界字段；有模板不强制开子 Agent → `.cursor/rules/nr-agent-session-kits-multi-agent-brief.mdc`
- 可选会话复盘——用户要求、卡住刹车上报或明显拉扯/高成本时按已安装 session-retro 模板写短表 → `.cursor/rules/nr-agent-session-kits-session-retro.mdc`
- 可选任务运行时——对齐复杂任务、逐票验证并保持长会话方向稳定 → `.cursor/rules/nr-agent-task-runtime-task-runtime.mdc`
- Codex 叶子 Agent 团队——按冻结边界选择探索、微型实现、局部实现或独立审查角色 → `.cursor/rules/nr-codex-agent-team-codex-agent-team.mdc`
- 共享交付门禁——按三路径审计稳定候选、证据、审查和 Git 契约 → `.cursor/rules/nr-delivery-gate.mdc`
- Codex Agent adapter——经 stdin `-` 同步调用 Codex CLI 并返回请求侧收据 → `.cursor/rules/nr-host-agent-adapter-codex-agent-adapter.mdc`
- Cursor Agent adapter——同步执行或只读审查合格叶子并返回最小事实收据 → `.cursor/rules/nr-host-agent-adapter-cursor-agent-adapter.mdc`
- 共享规则维护——规则变更后同步项目路由、引用与托管状态 → `.cursor/rules/nr-rules-maintenance.mdc`
- 共享卡住处理——假停、待决账本与有界重试；不定义调度或 Git 授权 → `.cursor/rules/nr-stall-handling.mdc`

项目私有规则、命令、架构与测试映射仍是项目具体细节的真源；它们负责补充项目参数，不应静默弱化共享安全或隔离约束。所需项目资源缺失但参数可由现有规则唯一确定时，Agent 应补齐并验证后继续；存在真实团队选择时再停止询问。确需偏离共享约束时须明确写出范围与理由。
<!-- rulesctl:managed:end -->
