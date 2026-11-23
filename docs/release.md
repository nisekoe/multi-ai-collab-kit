# 发布与自动更新（维护者视角）

> 客户端脚本：`tools/kit_update.py`；清单示例：`kit-manifest.example.json`。
> 原则：不静默替换、不绕版本门槛、不留半装状态。

## 一、发一版要准备什么

1. 打包：把仓库内容打成 `kit-<版本>.zip`。
2. 算摘要与大小：`sha256` 与字节数写进清单。
3. 在仓库根放 `kit-manifest.json`（字段见 `kit-manifest.example.json`）：

```json
{
  "kit": "multi-ai-collab-kit",
  "version": "0.3.0",
  "channel": "stable",
  "releasedAt": "2026-11-24",
  "minDsh": "0.4.0",
  "assets": [{"name": "kit-0.3.0.zip", "sha256": "…", "size": 123456}],
  "plugins": [],
  "notes": "一句话说明这版改了什么"
}
```

4. 在 GitHub Releases 建同名标签、传 ZIP，并把清单正文贴进发布说明（双份，防 raw 缓存）。

## 二、客户端怎么用

```bash
python tools/kit_update.py --check                       # 只读：有没有新版
python tools/kit_update.py --apply --yes                 # 更新（不加 --yes 会先问）
python tools/kit_update.py --status                      # 本地版本与上次检查时间
python tools/kit_update.py --rollback                    # 回到上一版
```

常用参数：`--manifest-url` 指定清单地址、`--install-dir` 指定安装目录、`--dsh-version` 传当前 DSH 版本（用来比 `minDsh`）、`--installed-version` 给没有版本记录的旧安装补一个起点。

## 三、安全边界（脚本里已经这么做）

- 只接受 HTTPS 清单与附件；清单大小有上限。
- 校验两样：附件 `sha256`＋`size`，以及 `minDsh`；任一不符就拒装并说明。
- 下载到同盘临时目录解包，先跑包内 `--help` 自检，通过后才原子换名。
- 换名或写版本记录失败会恢复旧目录；始终保留一个上一版备份供回滚。
- 连不上就安静跳过（每天最多查一次），不刷屏、不重试轰炸。

## 四、还没做的

- 清单签名（目前是「可信 HTTPS」级别，不做签名校验）。
- 真实 Release 与隔离 DSH 环境的端到端验收（脚本自带离线模拟测试，见 `tests/test_kit_update.py`）。