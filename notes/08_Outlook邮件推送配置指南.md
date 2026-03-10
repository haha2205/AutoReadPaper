# Outlook 邮件推送配置指南

> **更新日期**: 2026-03-10

---

## 问题说明

从你的日志看到邮件发送失败：
```
ERROR | Email send failed: (535, '5.7.139 Authentication unsuccessful, basic authentication is disabled.')
```

**原因**：微软 Outlook/Hotmail 已经禁用了基本身份验证（使用普通密码登录 SMTP），必须使用以下两种方式之一：
1. **应用专用密码**（推荐，最简单）
2. OAuth2 认证（复杂，需要修改代码）

---

## 解决方案1：使用应用专用密码（推荐）

### 步骤1：启用两步验证

1. 访问 **https://account.microsoft.com/security**
2. 登录你的 Outlook 账号（joezxq227@outlook.com）
3. 找到并点击 **"高级安全选项"** 或 **"More security options"**
4. 在 **"两步验证"** 或 **"Two-step verification"** 部分：
   - 如果显示 **"关闭"**，点击 **"设置两步验证"** 并启用
   - 按照提示绑定手机号或认证器应用

### 步骤2：生成应用密码

1. 两步验证启用后，返回 **https://account.microsoft.com/security**
2. 找到 **"应用密码"** 或 **"App passwords"** 部分
3. 点击 **"创建新的应用密码"** 或 **"Create a new app password"**
4. 输入名称（例如：AutoReadPaper）
5. 系统会生成一个 **16 位的应用密码**（格式类似：abcd-efgh-ijkl-mnop）
6. **复制这个密码**（只显示一次！）

### 步骤3：更新 .env 文件

打开 `project/.env`，将 `EMAIL_SMTP_PASSWORD` 修改为刚才生成的应用密码：

```env
EMAIL_SMTP_HOST=smtp.office365.com
EMAIL_SMTP_PORT=587
EMAIL_SMTP_USER=joezxq227@outlook.com
EMAIL_SMTP_PASSWORD=abcd-efgh-ijkl-mnop  # ← 替换为你的16位应用密码
EMAIL_FROM=joezxq227@outlook.com
EMAIL_TO=joezxq227@outlook.com
```

### 步骤4：重启容器

```powershell
docker compose restart paper-api
```

### 步骤5：测试邮件推送

在 n8n 中手动触发工作流，查看日志是否显示：
```
INFO | Email sent to ['joezxq227@outlook.com'] (20 papers).
```

然后检查你的 Outlook 邮箱收件箱。

---

## 解决方案2：使用 Gmail（备选方案）

如果你有 Gmail 账号，配置会更简单：

### Gmail 应用专用密码

1. 访问 **https://myaccount.google.com/security**
2. 启用 **"两步验证"**
3. 在 **"应用专用密码"** 中生成新密码
4. 更新 `.env`：

```env
EMAIL_SMTP_HOST=smtp.gmail.com
EMAIL_SMTP_PORT=587
EMAIL_SMTP_USER=your@gmail.com
EMAIL_SMTP_PASSWORD=your-16-digit-app-password
EMAIL_FROM=your@gmail.com
EMAIL_TO=joezxq227@outlook.com  # 接收邮箱可以是任意邮箱
```

---

## 解决方案3：使用 QQ 邮箱（国内推荐）

QQ 邮箱配置最简单，无需应用密码：

1. 登录 **https://mail.qq.com** → 设置 → 账户
2. 找到 **"POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务"**
3. 开启 **"IMAP/SMTP服务"**
4. 按提示发送短信验证后，会生成一个 **授权码**（16位）
5. 更新 `.env`：

```env
EMAIL_SMTP_HOST=smtp.qq.com
EMAIL_SMTP_PORT=465  # QQ邮箱使用SSL端口465
EMAIL_SMTP_USER=your@qq.com
EMAIL_SMTP_PASSWORD=your-16-digit-auth-code  # 授权码
EMAIL_FROM=your@qq.com
EMAIL_TO=joezxq227@outlook.com
```

---

## 常见问题

### Q1: 我没有看到"应用密码"选项

**A**: 可能原因：
1. 两步验证未启用（必须先启用）
2. 使用的是工作/学校账号（由管理员控制）
3. 你的账号类型不支持（切换到个人 Microsoft 账号）

### Q2: 应用密码生成后忘记保存了

**A**: 删除旧的应用密码，重新生成一个新的。

### Q3: 使用应用密码后仍然失败

**A**: 检查以下几点：
1. 确认 SMTP 服务器和端口正确
   - Outlook: `smtp.office365.com:587`
   - Hotmail: `smtp-mail.outlook.com:587`
2. 确认 EMAIL_SMTP_USER 是完整的邮箱地址
3. 检查防火墙是否拦截了 587 端口
4. 尝试在浏览器访问 https://outlook.office365.com 测试账号是否正常

### Q4: GitHub Issues 推送成功了，为什么选择它？

**A**: GitHub Issues 的优势：
- ✅ 永久存档，可搜索
- ✅ 支持 Markdown 格式化
- ✅ 可以打标签分类
- ✅ 支持评论和讨论
- ✅ 与代码仓库联动

你可以访问 https://github.com/haha2205/paper-archive/issues/1 查看推送的论文。

---

## 推荐配置

**最佳实践**：同时启用多个推送渠道

```yaml
# config.yaml
push:
  channels:
    email: true           # 日常查看
    github_issue: true    # 长期归档
    telegram: false       # 移动端即时通知（可选）
```

这样既能在邮箱中快速浏览，又能在 GitHub 中长期保存和搜索。

---

## 下一步

1. ✅ 按照上述步骤生成 Outlook 应用专用密码
2. ✅ 更新 `.env` 文件
3. ✅ 重启容器：`docker compose restart paper-api`
4. ✅ 在 n8n 中测试工作流
5. ✅ 检查邮箱和 GitHub Issues

如果仍有问题，可以查看容器日志：
```powershell
docker compose logs -f paper-api | Select-String "Email"
```
