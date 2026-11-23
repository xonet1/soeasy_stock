# 推送到 GitHub 操作指引（beta0.1）

仓库已在 `E:\workbuddy\stock-p` 初始化完成并完成首次提交（commit `e5c423d`），
工作区干净、无敏感数据。按下面步骤推送即可。

---

## 一、在 GitHub 网页端创建空仓库

1. Owner：`xonet1`，Repository name：`soeasy_stock`
2. Description 建议填：
   `本地股票分析工作台：通达信盘后数据 + 12维投资决策引擎 + 多账户组合分析 + 加密持仓。Windows 一键安装。`
3. Visibility 选 Public
4. Add README = Off、Add .gitignore = No gitignore、Add license = No license
   （本地已备齐这些文件，勾选会冲突）
5. 点 Create repository

---

## 二、推送命令（在本地 PowerShell 或 Git Bash 执行）

```bash
cd E:/workbuddy/stock-p

# 关联远程仓库（本地已配置好，无需修改）
git remote add origin https://github.com/xonet1/soeasy_stock.git

# 确认分支名为 main
git branch -M main

# 推送（-u 建立上游关联，以后直接 git push 即可）
git push -u origin main
```

推送时会弹出 GitHub 登录窗口，用浏览器完成授权即可。
若用的是个人访问令牌（PAT），密码处粘贴 token（需勾选 repo 权限）。

---

## 三、推送后建议补充（可选）

1. **打 tag 便于 Release**：
   ```bash
   git tag -a v0.1.0-beta -m "首个公开预览版 beta0.1"
   git push origin v0.1.0-beta
   ```
2. **创建 Release**：进仓库 → Releases → Draft a new release → 选 tag `v0.1.0-beta`，
   Release 说明可直接粘贴 `RELEASE_NOTES.md` 的内容。
3. **设置仓库主题标签**：在仓库页右侧 About 添加 topics，如
   `stock`, `python`, `flask`, `technical-analysis`, `echarts`, `a-share`。
4. **置顶 Issue 模板**：已在 `.github/ISSUE_TEMPLATE/` 备好 bug 报告与功能建议模板，
   推送后自动生效。

---

## 四、发布内容核对清单（推送前已确认）

- [x] 无真实持仓数据（`/api/holdings` 在干净包启动后返回空）
- [x] 无密钥、密文、`.bak` 备份（密钥本就不在项目内）
- [x] 无决策历史、盯盘列表、选股历史
- [x] 无 1 万个运行时缓存文件（cninfo/controller/sentiment 等）
- [x] 无审计日志、测试数据
- [x] 已泛化品牌表述为"通达信内核券商客户端"
- [x] 携带公开的全市场股票名称表（256KB，无个人信息）
- [x] Windows 一键安装脚本（winget + venv + 阿里云镜像 + 桌面快捷方式）
- [x] README 模仿 InStock 风格，含功能介绍/技术栈/安装说明/特别声明
- [x] Apache 2.0 许可证、CHANGELOG（RELEASE_NOTES）、Issue 模板齐备
- [x] Python 全量语法检查通过，隔离启动验证通过（8898 端口，服务健康、持仓为空）

---

## 五、后续版本计划（README 已埋点）

- beta0.2：Linux / macOS 支持（需解决本地日线数据源替代方案）
- beta0.3：可选的联网数据源（当前技术分析纯本地，联网模块仅舆情/股东等）
