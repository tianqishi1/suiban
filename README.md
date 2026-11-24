# 随办（Suiban）

基于开源项目 [Mika](https://github.com/wangzan101/Mika)（Apache-2.0）二次开发的移动端 AI 工作助手：
**屏幕顾问 + 手机执行器**。完整方案见 `docs/product-tech-design.md`。

## 目录结构

```
suiban/
├── docs/product-tech-design.md   # 产品设计 + 技术实现方案
├── packs/                        # Mika 插件格式场景包（可直接导入原版 Mika 验证）
│   ├── suiban.reply-advisor/     # 消息回复顾问（3 条候选回复，微信不代发）
│   ├── suiban.screen-extract/    # 屏幕信息抽取（订单/商品/名片/聊天要点 → 表格）
│   └── suiban.patrol-report/     # 定时巡检播报（只报变化，≤5 轮/次）
└── gateway/                      # 托管模型网关（OpenAI 兼容代理 → 百炼）
    ├── app.py                    # 设备令牌 / 模型别名路由 / 计量 / 预算熔断 / Mock
    ├── test_gateway.py           # 冒烟测试（13 项，无需真实 Key）
    ├── requirements.txt
    └── .env.example
```

## 网关快速开始

```powershell
cd D:\codex\suiban\gateway
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # 已装好可跳过
# 配置环境变量（参考 .env.example）：DASHSCOPE_API_KEY、SUISBAN_ADMIN_TOKEN
.\.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8800
```

签发设备令牌（令牌只显示一次）：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8800/admin/devices `
  -Headers @{"x-admin-token"="<SUISBAN_ADMIN_TOKEN>"} `
  -ContentType "application/json" -Body '{"name":"my-phone","days":30}'
```

冒烟测试（Mock 模式，不花钱）：`.\.venv\Scripts\python.exe test_gateway.py`

## M0 验证手册（2 周，原版 Mika 即可，无需 fork）

1. **装 Mika**：目标机型安装官方 APK，完成无线调试配对向导。
2. **接网关**：Mika 设置 › 模型 › 添加服务商 › 自定义（OpenAI 兼容）：
   - 地址 `http://<电脑局域网IP>:8800/v1`（手机电脑同一 Wi-Fi），Key 填上一步签发的设备令牌；
   - 「测试并保存」会读到三个模型：`suiban-main` / `suiban-vision` / `suiban-light`；
   - 用途分配：主模型=suiban-main，看图=suiban-vision，轻量=suiban-light；
   - 「上下文与压缩」触发点填窗口的 65%；主模型思考强度调低档。
3. **导入场景包**：把 `packs/` 三个目录传到手机，在 Mika 插件/技能入口导入（具体入口随版本核对）。
4. **跑三场景**：回复顾问（双击胶囊）、信息抽取（订单页）、巡检（配一个每日任务）。
5. **记数据**：每场景 ≥20 次，记录成功率、时延、token（Mika 任务小结 + 网关 /admin/usage）。
6. **退出标准**：顾问采纳率 ≥40%、执行成功率 ≥85%、顾问 ≤0.02 元/次、执行 ≤0.3 元/任务。达标 → 启动 M1 fork；不达标 → 止损或换场景。

## 状态

- [x] 三个场景包（Mika 插件格式，skills-only）
- [x] 托管模型网关 v0.1（13/13 冒烟测试通过）
- [x] 产品与技术实现方案文档
- [ ] M0 真机验证（需要：目标机型 + 百炼 API Key）
- [ ] M1：fork 换壳、建议卡片 UI、AdvisorTurn、RemoteSync、团队控制台

## License

场景包与网关代码自有；Mika 上游 Apache-2.0，fork 时保留 LICENSE/NOTICE。
