# Gym-Anything AGS Runtime 使用和进展说明

日期：2026-06-04

这份文档记录当前仓库中 AGS runtime 的使用方式，以及 CUA-World registry verified 子集在 AGS 沙箱上的验证进展。

## 当前结论

当前生产候选基线是：

- `1154` 个 registry verified、Ubuntu/container 方向的任务完成验证。
- `1083` 个任务通过 `post_start`、`setup_task` 和 mock verifier 合约。
- `71` 个任务失败并保留为失败，不掩盖 hook failure。
- 通过任务覆盖 `26` 个 env。
- 这不是实际 LLM rollout 得分，只验证环境启动、任务 setup 和 verifier 调用链路。

当前候选 AGS tool：

- Tool: `ags-cua-blender-r-202606041920`
- Tool ID: `sdt-8sbro0z3`
- Region: `ap-guangzhou`
- Image: `ccr.ccs.tencentyun.com/pengdrumli/ags-ga-cua-world-full:registry-highres-nodocker-main-fastpost-eclipse-maven-blender-r-20260604-1918`
- Image digest: `sha256:1334124b96d9bff1b6136a62003906f396cf5f7660cda54f0eccf7fbe7516396`
- Local image ID: `sha256:ead2c0cdee340a033a44b576887928a8833fac6df8ecc2097ca797c53c62f594`
- Local image size: `12,824,603,628` bytes，Docker 显示约 `12.8GB`。
- Resources: `8 CPU / 16Gi`
- Network: `PUBLIC`
- envd port: `49983`
- Probe: `HTTP /health` on port `49983`

当前通过任务的平均初始化耗时：

- `sandbox_create_sec`: 平均约 `6.16s`。
- `post_start_sec`: 平均约 `22.31s`，中位数约 `5.05s`。
- `pre_task_sec`: 平均约 `31.01s`，中位数约 `14.49s`。
- `reset_sec`: 平均约 `63.95s`，中位数约 `39.92s`，P95 约 `203.51s`。

训练等待更接近 `reset_sec`，不是单纯的沙箱创建时间。慢尾主要来自少数 GUI 或数据准备较重的 env。

## AGS Runtime 做了什么

新增 runner：

- `src/gym_anything/runtime/runners/ags.py`

`AGSRunner` 通过 E2B/envd 兼容接口使用 AGS custom sandbox：

- 通过 `e2b.Sandbox.create(...)` 创建 AGS 沙箱。
- 通过 E2B command API 执行 shell 命令。
- 通过 E2B file API 上传 env/task mounts。
- 默认跳过 `pre_start`，因为 AGS 镜像应预先 bake 大型依赖。
- 支持截图、文件上传下载、鼠标键盘动作注入、task hook 执行和 verifier 调用。

相关 runtime 改动：

- `GYM_ANYTHING_RUNNER=ags` 或 env spec 中 `runner: ags` 会选择 `AGSRunner`。
- `gym-anything doctor` 支持检查 AGS 依赖。
- Linux hook 现在会正确 shell quote，并能透传 `GYM_ANYTHING_FAST_POST_START`。
- `EnvSpec.runner` 和 runner compatibility 增加 `ags`。
- 新增 `MockDoneAgent` 供 smoke 测试使用。

注意：当前 AGS CUA 桌面沙箱不是 E2B code interpreter sandbox，不能依赖 code-run 接口执行任务；本实现走 command/file API。

## 环境变量

最小必需环境变量：

```bash
export E2B_API_KEY=...
export E2B_DOMAIN=ap-guangzhou.tencentags.com
export GYM_ANYTHING_AGS_TEMPLATE=ags-cua-blender-r-202606041920
```

常用可选变量：

```bash
export GYM_ANYTHING_RUNNER=ags
export GYM_ANYTHING_AGS_DISPLAY=:1
export GYM_ANYTHING_AGS_CREATE_TIMEOUT=300
export GYM_ANYTHING_AGS_CREATE_RETRIES=2
export GYM_ANYTHING_AGS_TIMEOUT=3600
export GYM_ANYTHING_FAST_POST_START=1
```

如果需要保留沙箱用于调试：

```bash
export GYM_ANYTHING_AGS_KEEP_ALIVE=1
```

依赖：

```bash
pip install e2b
```

## 基础检查

检查 runner 依赖：

```bash
PYTHONPATH=src:. gym-anything doctor --runner ags
```

或者运行相关单元测试：

```bash
PYTHONPATH=src:. python3 -m pytest \
  tests/test_env_runtime_behaviors.py \
  tests/test_runner_execution_contracts.py
```

当前已验证结果：

- `14 passed`
- `1 skipped`

## Smoke 测试脚本

新增脚本：

- `scripts/ags_full_mock_smoke.py`

它会：

- 从 parquet 或 CUA-World registry 中选择任务。
- 为 AGS runner 重写 env mounts。
- 上传任务目录、脚本、数据和 env 配置到 AGS 沙箱。
- 使用 `MockDoneAgent` 立即返回 done。
- 执行 `post_start`、`setup_task` 和 verifier。
- 将结果写入 `results.jsonl` 和 `summary.json`。

示例：列出 registry verified 任务，不运行沙箱：

```bash
PYTHONPATH=src:. python3 scripts/ags_full_mock_smoke.py \
  --task-source registry \
  --surface verified \
  --split all \
  --all-envs \
  --exclude-env docker_env \
  --template "$GYM_ANYTHING_AGS_TEMPLATE" \
  --list-only
```

示例：并发运行指定 env 的 smoke：

```bash
PYTHONPATH=src:. python3 scripts/ags_full_mock_smoke.py \
  --task-source registry \
  --surface verified \
  --split all \
  --env libreoffice_calc_env \
  --template "$GYM_ANYTHING_AGS_TEMPLATE" \
  --concurrency 32 \
  --fast-post-start \
  --run-id libreoffice_calc_ags_smoke
```

示例：重跑某个失败清单：

```bash
PYTHONPATH=src:. python3 scripts/ags_full_mock_smoke.py \
  --task-source registry \
  --surface verified \
  --split all \
  --only /path/to/failed.jsonl \
  --template "$GYM_ANYTHING_AGS_TEMPLATE" \
  --concurrency 32 \
  --fast-post-start \
  --run-id retry_failed_tasks
```

## 当前验证进度

当前基线来自多轮 AGS smoke 和定向修复：

1. 起点：扩展到 `1154` 个 registry verified 候选任务。
2. DBeaver 修复：加入本地 `northwind.db` fixture，修正 Northwind/Chinook SQL schema 兼容问题。
3. Eclipse/Maven 修复：补齐 Eclipse、Maven、Java/Node 相关启动和 task setup 问题。
4. Blender/R 修复：补齐 Blender runtime、R 依赖和相关 task setup 脚本问题。
5. 当前结果：`1083 / 1154` 通过，剩余 `71` 失败。

按 GA registry split 反查，当前 `1083` 个通过任务的划分是：

- Train: `867`
- Test: `216`
- Total: `1083`

当前 validated manifest 中的 `split` 字段统一写为 `all`；上面的 train/test 数量是用 `env + task_id` 回查 `benchmarks/cua_world/splits/*_split.json` 得到的。没有 unmapped task，也没有 train/test 重叠。

已提交到仓库的主要修复：

- `Add AGS runner support`
- `Stabilize DBeaver CUA tasks`
- `Harden CUA-World desktop task setup`

## 当前通过任务分布

| Env | 通过任务数 |
|---|---:|
| AstroImageJ (`astroimagej_env`) | 2 |
| Blender (`blender3d_env`) | 54 |
| BlueMail (`bluemail_env`) | 18 |
| CAMEO Chemicals (`cameo_chemicals_env`) | 80 |
| DBeaver (`dbeaver_env`) | 77 |
| Eclipse (`eclipse_env`) | 69 |
| Fiji/ImageJ (`fiji_env`) | 40 |
| Firefox (`firefox_env`) | 72 |
| GeoGebra (`geogebra_env`) | 71 |
| Google Earth (`google_earth_env`) | 87 |
| HEC-RAS (`hec_ras_env`) | 48 |
| LibreOffice Base (`libreoffice_base_env`) | 78 |
| LibreOffice Calc (`libreoffice_calc_env`) | 189 |
| LibreOffice Impress (`libreoffice_impress_env`) | 27 |
| LibreOffice Writer (`libreoffice_writer_env`) | 74 |
| Microsoft Edge (`microsoft_edge_env`) | 7 |
| OpenICE (`openice_env`) | 7 |
| OpenLCA (`openlca_env`) | 8 |
| RStudio (`rstudio_env`) | 6 |
| Stellarium (`stellarium_env`) | 8 |
| SUMO (`sumo_env`) | 15 |
| System Advisor Model (`system_advisor_model_env`) | 10 |
| Thunderbird (`thunderbird_env`) | 10 |
| Weasis (`weasis_env`) | 15 |
| WPS Writer (`wps_office_writer_env`) | 2 |
| WPS Spreadsheet (`wps_spreadsheet_env`) | 9 |

总计：`1083` 个任务。

WPS Writer 任务少不是当前筛选丢失导致的；GA registry verified 中 `wps_office_writer_env` 本身只有两个任务：

- `create_data_table`
- `legal_contract_redline`

这两个都属于 train split，test split 为 `0`。

## 剩余失败分类

剩余 `71` 个失败任务按主要原因分类：

| 类型 | 任务数 | 涉及 env | 当前建议 |
|---|---:|---|---|
| 缺核心 app/runtime 或官方镜像层 | 53 | `fiji_env` 17, `hec_ras_env` 17, `google_earth_env` 17, `bluemail_env` 1, `rstudio_env` 1 | bake 真实软件/官方资产，或排除 env |
| 当前地域外网下载不可达 | 10 | `snap_env` 8, `astroimagej_env` 1, `geogebra_env` 1 | 新加坡地域重测，不在广州结果中直接判死 |
| 缺官方任务 artifact/data | 3 | `astroimagej_env` 1, `twon_access_commander_env` 2 | bake 官方数据/OVA，或排除 |
| 数据可达后仍可能有 GUI readiness 问题 | 5 | `snap_env` 5 | 先解决数据可达性，再单独修 SNAP 启动 |

目前不做合成样本替代生产数据；外网下载失败先记录，不在广州环境中强行修复。

## Fiji Runtime 进展

Fiji/ImageJ 的无合成样本 runtime 已单独构建并在 AGS 沙箱中验证：

- Tool: `ags-cua-fiji-runtime-nosamples-202606042039`
- Tool ID: `sdt-bbtyys9x`
- Image: `ccr.ccs.tencentyun.com/pengdrumli/ags-ga-cua-world-full:registry-highres-nodocker-main-fastpost-eclipse-maven-blender-r-fiji-runtime-nosamples-20260604-2039`
- Digest: `sha256:3a13c9a0b62395e80053f3a8f956105df3d84115e5c0653515daa15d178673d5`
- Local image ID: `sha256:1a54230c6e12ac6f40c2d5af3a1e03dd0cd5ac812fb496b55dbadd2dea0f79d1`
- Local image size: Docker 显示约 `13GB`。

已验证：

- `fiji` 和 `imagej` 存在。
- `/opt/fiji/ImageJ-linux64` 可执行。
- `skimage` 可 import。
- `/opt/fiji_samples` 不存在。

这说明 Fiji runtime 侧已经可用，但官方样本数据和下载可达性仍未解决。因此该结果没有合并进 `1083 / 71` 基线。

## 当前建议

短期生产候选：

- 使用 `1083` 个已通过任务作为 no-synthetic baseline。
- 暂时排除需要 Docker/VM 的 env。
- 暂时不强行修复大量失败 env，先保留失败原因清单供评估。

下一步可选扩展：

- 在新加坡地域重测下载敏感 env：`snap_env`、`geogebra_env`、Fiji 下载依赖任务、AstroImageJ 下载任务、RStudio 数据下载。
- 对 `hec_ras_env`、`google_earth_env`、`bluemail_env`、`rstudio_env` 评估是否值得 bake 专用应用层。
- 对 `twon_access_commander_env` 决定是否允许 OVA/VM 类任务进入当前 profile。
