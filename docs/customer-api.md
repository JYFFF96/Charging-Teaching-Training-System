# 客户 API 约定（待后台实现）

所有接口使用 HTTPS 和 JSON；除注册、登录、找回流程外，使用 `Authorization: Bearer <客户Token>`。客户 Token 不得具有管理权限。桩号和枪号不是身份凭证。

| 方法 | 路径 | 请求/用途 |
|---|---|---|
| POST | /api/customer/register | username,password,security_question_id,security_answer；创建客户 |
| POST | /api/customer/login | username,password；返回 token、expires_at、用户信息 |
| POST | /api/customer/logout | 注销 Token |
| POST | /api/customer/recovery/question | username；返回问题及短期 challenge_id |
| POST | /api/customer/recovery/verify | challenge_id,security_answer；返回短期一次性 reset_token |
| POST | /api/customer/recovery/reset | reset_token,new_password；重设密码并注销旧会话 |
| GET | /api/customer/piles/{pile_no}/guns/{gun_no} | 在线、插枪、故障状态，实时数据及当前收费规则 |
| POST | /api/customer/orders | pile_no,gun_no,idempotency_key；创建本人订单 |
| POST | /api/customer/orders/{id}/start | 后台核验订单、支付/余额及设备状态后启动 |
| POST | /api/customer/orders/{id}/stop | 停止本人订单，等待真实设备结果 |
| GET | /api/customer/orders/{id} | 本人订单状态及实时信息 |
| GET | /api/customer/orders | 分页返回本人历史及进行中订单 |

密码和安全答案分别加盐哈希；不返回原密码或答案。限制登录和安全答案验证失败次数，对注册及恢复接口限流；找回密码避免暴露账号是否存在。重置令牌需过期、一次性消费且绑定账号。订单价格、余额、用户身份及权限均由后台计算验证，不能信任前端传值。

订单状态建议：created、starting、charging、stopping、finished、failed、unknown。设备超时代表结果未知，不直接等同失败；重试需要幂等，并通过设备实时上报和交易记录核实。价格单位明确为元/kWh，金额使用整数分或十进制定点数。实时设备数据注明更新时间。

原版 /api/login 和 /api/piles/... 是管理接口，本文件的客户接口尚未实现。
