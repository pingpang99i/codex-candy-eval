# Codex 降智测试

用本地 Codex CLI 批量测试一道糖果数学题，并统计 reasoning tokens 与正确率。

![example](./example.png)

## 用法

该脚本无任何第三方依赖，只需要您已安装并登录 [Codex CLI](https://github.com/openai/codex)

```bash
python codex_candy_eval.py -m gpt-5.5 -r high -n 5
```

模型名不由本项目维护白名单，`-m` 会原样传给本地 Codex CLI。当前 CLI 识别的模型及其推理档位可动态查看：

```bash
python codex_candy_eval.py --list-models
```

目录中出现模型不等于当前账号或自定义 provider 一定有调用权限，实际可用性仍由 Codex 请求结果决定。

### 一键运行
以下任选其一
```bash
wget -qO- "https://raw.githubusercontent.com/haowang02/codex-candy-eval/main/codex_candy_eval.py" | python3 - -m gpt-5.5 -r high -n 5
```
```bash
curl -fsSL "https://raw.githubusercontent.com/haowang02/codex-candy-eval/main/codex_candy_eval.py" | python3 - -m gpt-5.5 -r high -n 5
```


参数：

- `-m, --model`：codex 模型名，省略则用本地默认
- `-r, --reasoning-effort`：透传给 Codex，支持的值随模型而定（默认 `medium`）
- `-n, --tests`：测试次数（默认 1）
- `-t, --timeout`：每次测试的超时秒数（默认 180，设为 0 可关闭）
- `--list-models`：列出本地 Codex CLI 当前报告的模型和推理档位后退出

每次测试开始时会立即向 stderr 输出所用模型、推理档位和超时；请求未结束时每 10 秒输出一次进度，避免把正常的长推理误判为程序无响应。失败的完整原因也会写入 stderr，stdout 继续只输出汇总表，并以非零状态退出。

正确答案为 **21**，脚本直接判断回答中是否出现独立的 `21`。

## 致谢

- [LINUX DO](https://linux.do/) - 新的理想型社区
