# Charging Teaching Training System

## 一体化版本

只运行 `server/ykc_platform_server.py`，同一进程启动：

| 默认端口 | 功能 |
|---|---|
| 8768 | 云快充V1.6设备TCP通信 |
| 8000 | 原后台管理页面、管理REST和WebSocket |
| 8088 | 客户页面、账号、订单、设备状态和客户启停 |

不再单独启动customer_web.py，旧文件仅保留兼容历史版本。
客户用微信扫一扫或手机相机扫描带参数URL，网页不调用摄像头。
`http://182.92.85.98:8088/?pile_no=32010200000001&gun_no=1`

## 客户行为

- 未登录先注册/登录；保留扫码桩号和枪号。
- 本人存在进行中订单：直接恢复状态；其他账号：只显示正在使用，不暴露他人订单。
- 空闲、最新数据有效且插枪：客户确认后下发真实0x34。
- 已充电但没有客户订单：显示占用，不允许认领或停止。
- 数据超过90秒、桩断线、故障或无最新数据：不允许启动。
- 设备回复超时/连接中断：保留订单和枪占用，不重复启动。
- 成功停机回复不等于交易结束；保留占用直至匹配0x3B交易上报。
- 同桩同枪最多一个进行中订单，SQLite唯一索引及会话枪锁防并发冲突。
- 订单与设备交易流水号、桩号、枪号匹配。客户仅可查询/停止自己的订单。
- 账号、会话和订单持久化，数据库沿用customers.sqlite3；本轮联调清空后需要重新注册。

## 支付与HTTP

本版不收款、不自动扣款。启停现在会控制真实设备。下发额度由后台 `--customer-test-balance-fen` 设置，单位分，默认100000分（1000元），只用于设备联调，并非客户充值余额。前端不能自行改变额度。交易金额来自桩报文。未实现支付验证，不可当成收费运营版本。

按当前需求支持直接HTTP访问，无需证书，也无网页扫码。HTTP账号和凭证未加密。保留可选 `--customer-https` 供未来HTTPS代理使用。

## 当前服务器更新

1. 备份 `/root/ykc_platform_server.py`、`/root/charge.html` 和 `/root/customer_data/customers.sqlite3`（先停止写入再复制数据库）。
2. 将仓库 `server/ykc_platform_server.py` 上传为 `/root/ykc_platform_server.py`；`customer/charge.html` 上传为 `/root/charge.html`。
3. 原后台 `/root/index.html` 保留，不替换；customer_data、stations.json、transactions.json、faults.json保留。
4. 检查 `ss -ltnp '( sport = :8088 or sport = :8000 or sport = :8768 )'`，确认PID后停止旧客户服务和旧设备后台。若由supervisor/systemd管理，在原管理器停止，防止自动重启。不要停止宝塔Nginx。
5. 启动一体化服务。保留原后台管理员账号密码和其他已有参数，示例如下（占位账号密码必须替换）：

```bash
nohup python3 -u /root/ykc_platform_server.py --port 8768 --web-port 8000 --customer-port 8088 --customer-page /root/charge.html --customer-db /root/customer_data/customers.sqlite3 --customer-test-balance-fen 100000 --admin-user YOUR_EXISTING_ADMIN --admin-pass 'YOUR_EXISTING_PASSWORD' > /root/ykc.out 2>&1 < /dev/null &
```

先在没有进行中充电时更新，避免断开设备连接及丢失命令等待。不要与旧后台同时运行。无需customer_web.py或额外Python依赖。设备重新登录及实时上报后，客户端才会允许启动。

```bash
ss -ltnp '( sport = :8088 or sport = :8000 or sport = :8768 )'
tail -n 30 /root/ykc.out
```

## 联调顺序

1. 原管理员登录仍正常，客户旧账号登录仍正常。
2. 桩在线且已插枪，用账号A进入扫码链接，确认设备信息后启动。
3. A刷新或再扫码：恢复本人订单；账号B扫码同枪：提示正在使用。
4. A停止，等待桩交易记录；确认最终金额、电量与后台一致。
5. 测试双枪、启动失败、断线、重启和超时迟到回复；结果未知订单不会自动释放，需要设备报文核实。

## 验证与局限

```bash
python -m unittest discover -s tests -v
```

账号HTTP接口与模拟设备协议测试10项通过；前端JavaScript语法检查通过。未在实际手机浏览器和真实充电桩验证本版本。安全问题仅为当前约定的恢复方式。

原后台卡启动、并充、其他管理命令沿用旧实现；客户仅支持单枪远程启动/停止。不提供手工强制释放未知订单按钮，避免误认空闲或停止别人的充电。充电桩若未发送匹配流水号的交易记录，需要核查设备报文。

## 清空客户测试数据

先停止一体化后台，再删除 `/root/customer_data/customers.sqlite3` 及同名 `-wal`、`-shm`、`-journal` 文件；重新启动会自动创建空库。此操作删除客户账号、会话、密保及客户订单，不删除后台 transactions.json、stations.json、faults.json。旧密保问题已移除，只保留新版8项。客户页面现名为 `customer/charge.html`，后台管理index.html不变。
