# 一体化客户API（8088）

客户服务由ykc_platform_server.py启动，共享实时设备会话。管理接口仍在8000，客户会话不能访问管理权限。页面及API同源，POST使用JSON、X-Customer-Request: 1及Origin；会话HttpOnly/SameSite Cookie。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /api/customer/config | HTTP模式、设备模式、测试额度（元） |
| GET | /api/customer/me | 当前客户 |
| POST | /api/customer/register | username,password,security_question_id,security_answer |
| POST | /api/customer/login | username,password |
| POST | /api/customer/logout | 注销当前会话 |
| POST | /api/customer/recovery/verify | username,security_question_id,security_answer，返回5分钟一次性reset_token |
| POST | /api/customer/recovery/reset | reset_token,new_password，撤销所有旧会话 |
| GET | /api/customer/device?pile_no=...&gun_no=... | 可用性/本人订单/实时数据/计费模型 |
| POST | /api/customer/orders | pile_no,gun_no；确认并启动；同账号同枪进行中订单返回原订单，不重复发命令 |
| GET | /api/customer/orders | 本人最近100笔订单 |
| GET | /api/customer/orders/{id} | 本人订单及匹配的最新设备数据 |
| POST | /api/customer/orders/{id}/stop | 停止本人订单，须最新充电数据与订单流水匹配 |

availability: idle、mine、occupied、offline、fault、unknown。其他账号占用只返回占用提示，不返回用户、流水、金额或订单。
订单状态: starting、charging、stopping、unknown、ending、finished、failed。starting/charging/stopping/unknown/ending均占用枪；只有明确启机失败或匹配交易上报释放。服务重启将starting/stopping改为unknown，不自动重新下发。实时充电报文须匹配流水、桩和枪才能更新本人订单；空闲报文不立即释放占用，等待最终交易记录。

单位: 实时energy/amount及最终total_energy/total_amount按报文值除10000，charge_time分钟。计费模型尖峰平谷电费和服务费除100000（元/kWh）。下发启动额度为可用模拟余额与后台单次上限的较小值，单位分；不接受前端余额输入。

账号4–32位字母数字下划线，大小写不敏感；密码8–128字符。问题ID movie、book、color、sport、disliked_food、first_phone_last4、respected_person、favorite_number。旧版密保问题已移除，注册和找回仅接受新版8项。安全答案允许1–128字符，支持单个数字或汉字。答案NFKC、去两端空白、大小写归一化，密码与答案独立加盐PBKDF2-SHA256。会话24小时，重置凭证5分钟且一次性。数据库包含原版账号表及新增orders表，清空联调数据库后须重新注册。

错误码401未登录/验证失败、404订单不存在（含非本人订单）、409占用/设备状态不允许、429请求过多。订单操作每个账号最多30次/10分钟。启动最长等待80秒，停止20秒；客户端请求超时不代表命令失败，应重新查询订单，不能重复自动启动。

真实支付尚未接入；模拟扣费由后台在最终账单到达时执行，前端无直接扣费权限。

## 管理员充电记录（8000）

GET /api/charging-records，使用原后台管理员Bearer Token鉴权；客户Cookie和客户Token没有访问权限。

可选参数：pile_no（部分匹配）、gun_no（1–99精确匹配）、username（账号不区分大小写部分匹配）、serial_no（部分匹配）、status、source（customer/device）、date_from/date_to（YYYY-MM-DD，按开始/创建日期，包含当天）、page（默认1）、page_size（默认50，最大100）。

返回records、total、page、pages、page_size和summary。summary为筛选范围的最终账单电量/金额，不包含未结束订单的实时估算；并非支付到账统计。列表进行中数据匹配实时流水，标注实时；最终账单覆盖实时估算。

客户订单与transaction_archive按流水号、桩号、枪号合并，重复上报不会重复计数。未关联订单的桩账单单独显示，username为空。返回账号和订单信息，不包含密码、密保或会话凭证。

启动自动从现存transactions.json和已保存客户订单账单导入历史；新账单归档在customers.sqlite3的transaction_archive表，不再受JSON最近5000条限制。已经被删除或截断且没有其他副本的历史无法恢复。清空整个customers.sqlite3会同时清空客户订单和该归档。

## 模拟钱包API

- 客户GET /api/customer/wallet：返回本人的wallet，含balance_fen、reserved_fen、available_fen、simulated=true及最近50条ledger；金额为整数分。
- 管理GET /api/customer-wallets?username=...&page=1&page_size=50：客户账户余额分页列表，管理员Token鉴权。
- 管理GET /api/customer-wallets/detail?username=...：指定账户钱包与流水。
- 管理POST /api/customer-wallets/adjust：username（完整账号）、action（credit/debit）、amount（元，正数字符串，最多2位小数）、note（1–200字）、request_id（8–80位字母数字下划线短横线）。单次0.01–1000000元。相同操作人+request_id重复同内容只执行一次，内容冲突409。可用余额不足拒绝管理员扣费。

wallets保存余额；wallet_ledger保存不可重复的变动key、delta_fen、before_fen、after_fen、kind、note、actor、order_id、created。每个账号首次开钱包赠送100000分，仅一次。订单新增reserved_fen、wallet_managed、settled_fen、settlement_status、fail_reason。

新订单启动预留额度；账单按 (total_amount + 50) // 100 转换为整数分结算（原单位0.0001元）。账单入档、订单完成、释放预留、钱包扣减和流水同事务提交。charge:<order_id>唯一，重复账单不重复扣；不同最终金额标review待核查。旧订单wallet_managed=0不追扣。未知结果订单保留预留，失败则释放预留、不扣费。

注册赠送bonus，管理员充值admin_credit、扣费admin_debit，充电结算charging。settlement_status：pending、settled、not_charged、legacy、review。原因码5的订单note明确提醒先拔枪再重新插枪。
