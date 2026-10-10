# Charging Teaching Training System

充电教学与实训系统：云快充协议后台及手机客户 H5。

## 目录

- `server/ykc_platform_server.py`：用户提供的后台原版，未修改协议逻辑。
- `customer/index.html`：独立客户 H5，注册、登录、安全问题找回、设备详情、演示充电及演示订单。
- `docs/customer-api.md`：后台同事需要实现的客户接口约定。

## 客户页面本地预览

安装 Python 3，在仓库根目录运行：

```bash
python -m http.server 8088 --directory customer
```

访问 `http://localhost:8088/?pile_no=32010200000001&gun_no=1`。
手机与电脑在同一网络时，可以用电脑局域网 IP 替换 localhost。

当前为明确标记的演示模式：账号密码和安全答案不保存、不发送；表单只演示流程，登录不校验真实身份；设备和充电数据为模拟值，不发送启停命令、不收款。刷新后演示状态清空。不要输入真实密码。

## 原版后台

```bash
python server/ykc_platform_server.py --host 0.0.0.0 --port 8768 --web-port 8080 --admin-user YOUR_ADMIN --admin-pass YOUR_STRONG_PASSWORD
```

后台管理首页需要另行将原有管理 `index.html` 放到 `server/`，本次未提供该文件。客户页面不要覆盖管理首页。原版后台只有管理员鉴权，不可作为客户账号系统直接使用。不要使用默认管理员密码。

## 阿里云部署客户页面

宝塔现有独立静态站点目录：`/www/wwwroot/182.92.85.98_8088`。
将 `customer/index.html` 上传到该目录。放行 TCP 8088 后可访问 HTTP 演示页面。不要修改其他业务站点。

正式客户登录、密码找回和支付必须通过 HTTPS；同源反向代理客户 `/api/customer/` 接口。正式版本应保留扫码参数，在有效登录状态下直接进入设备详情。

## 当前限制与下一步

1. 后台同事实现客户鉴权、限流、订单归属及数据库持久化。
2. 前端替换演示处理为客户 API，并添加超时、错误和订单恢复。
3. 修复原版设备命令应答竞争和双枪并发匹配，真实桩联调。
4. 接入收费规则、支付与最终结算，部署 HTTPS。

客户页面尚未对接真实后台；不能用于真实客户充电。
