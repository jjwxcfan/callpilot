## 中文

### 改了什么 / 为什么
<!-- 一两句说清问题与修法。若修 bug，请写清「根因」，不只是症状。 -->

### 怎么验证的
<!-- 交付级验证：测试全绿 ≠ 交付完成。 -->
- [ ] 三件套全绿：`.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/mypy`
- [ ] **变异验证**：删掉本次的生产接线，新增测试会变红（不是只断言私有字段）
- [ ] 碰通话链路的话：真机拨测过（只拨本卡运营商免费客服号）
- [ ] 改了配置键：`.env.example` 与 `config.py` 两边都改了
- [ ] 未提交任何密钥 / 真实号码 / 会话归档

### 硬约束自查
- [ ] 对话逻辑**未**使用关键词表 / 话术清单 / 号码→类型映射（非枚举原则）
- [ ] 不可逆动作（挂断 / 转接 / 发短信）由判官判断 + 代码执行，未交给对话模型自行决定

Refs #

---

## English

### What changed / why
<!-- One or two sentences. For a bug fix, state the root cause, not just the symptom. -->

### How it was verified
<!-- Delivery-grade: passing tests is not the same as delivered. -->
- [ ] Quality gate green: `pytest && ruff check . && mypy`
- [ ] **Mutation-checked**: deleting the production wiring turns the new tests red
- [ ] Real-hardware dial test, if this touches the call path (carrier's own free hotline only)
- [ ] Config key changes applied to both `.env.example` and `config.py`
- [ ] No secrets, real numbers, or session archives committed

### Hard-constraint check
- [ ] No keyword tables / phrase lists / number→category maps in conversation logic
- [ ] Irreversible actions gated by a judge + executed by code, not decided by the realtime model
