# Charging Teaching Training System

云快充设备后台与客户 H5。后台管理页面继续使用 8000；独立客户服务使用 8088。

## 已实现

- 客户注册、账号密码登录、注销及24小时登录会话。
- 注册时设置安全问题；验证问题与答案后获取5分钟一次性重置凭证。
- 重置密码注销该账号全部旧会话和重置凭证。
- SQLite持久化，密码和答案使用不同随机盐的PBKDF2-SHA256（600000轮）；数据库只保存会话令牌摘要。
- 账号及来源IP请求限流，同源JSON请求检查，HttpOnly/SameSite Cookie。
- 登录后保留URL中的桩号与枪号，页面刷新可恢复登录。

充电设备数据、启停及订单仍为前端演示，没有真实命令或支付。原版管理员接口没有开放给客户。

## 本地测试

Python 3.10+，仅标准库，无需pip依赖。

```bash
python server/customer_web.py --allow-insecure-test
```

打开 `http://127.0.0.1:8088/?pile_no=32010200000001&gun_no=1`。
不能再使用静态http.server运行真实账号接口。

```bash
python -m unittest discover -s tests -v
```

## 当前阿里云HTTP联调（仅虚构账号）

将 `server/customer_web.py` 上传到 `/root/customer_web.py`，将 `customer/index.html` 上传并命名为 `/root/charge.html`。
先备份旧文件。使用 `ss -ltnp 'sport = :8088'` 找到旧客户Python服务PID，确认后 `kill <PID>`；不要停止8000后台或其他Nginx服务。

```bash
nohup python3 -u /root/customer_web.py --host 0.0.0.0 --port 8088 --page /root/charge.html --db /root/customer_data/customers.sqlite3 --allow-insecure-test > /root/customer_web.log 2>&1 < /dev/null &
```

访问 `http://182.92.85.98:8088/?pile_no=32010200000001&gun_no=1`。
账号现在实际保存在数据库；HTTP不能保护密码传输，仅可使用专用虚构测试密码。正式客户上线需要HTTPS并移除 `--allow-insecure-test`。默认服务仅绑定127.0.0.1，适合由HTTPS反向代理提供访问；Secure Cookie仅在HTTPS下传输。反向代理需保留原Host。不要暴露数据库目录、不要将数据库提交到GitHub，更新代码不要删除数据库。

## 文件

- `server/ykc_platform_server.py`：上传的设备后台原版。
- `server/customer_web.py`：独立客户页面和账号服务。
- `customer/index.html`：部署时可命名为charge.html。
- `tests/test_customer_accounts.py`：账号生命周期与接口验证。
- `docs/customer-api.md`：接口与下一步说明。

后续实现真实设备查询、订单归属、充电命令关联、支付结算。当前不接入管理员Token。后台管理index.html未提供，继续使用服务器原文件。
