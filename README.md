# DirDiff Master

DirDiff Master 是一款 Python 桌面 GUI 文件夹对比工具，用于比较两个文件夹当前层级内的文件差异，并对"对比文件夹"中的差异文件执行移动或删除操作。

- 作者：Tobin
- 项目地址：https://github.com/caifugao110/dirdiff-master
- 开源协议：MIT

## 功能

- 支持本地路径和局域网 UNC 路径，例如 `\\192.168.160.10\GUNtools\Profile`。
- 区分“基准文件夹”和“对比文件夹”，适合旧文件夹与新文件夹对比。
- 支持包含子文件夹的递归比较（可选）。
- 默认比较全部文件名并包含文件格式后缀。
- 支持忽略文件格式后缀，只按文件名主体比较。
- 支持从两个文件夹动态获取文件格式，并只比较指定格式。
- 默认使用“文件大小 + 修改时间”的快速比较；可勾选精确内容校验。
- 对比完成后生成 CSV 和可筛选 HTML 报告。
- 支持打开本次生成的 HTML 报告。
- 支持移动或删除对比文件夹中的选中差异/全部差异文件。
- 支持一键清空路径选择和对比结果。
- GUI 支持多主题切换。

## 直接运行源码

```powershell
pip install -r requirements.txt
python .\app.py
```

## 构建单文件 exe

```powershell
.\scripts\build_exe.ps1
```

构建完成后只保留：

```text
dist\DirDiffMaster.exe
```

构建脚本会自动创建临时虚拟环境、安装依赖、生成图标、调用 PyInstaller，并在结束后清理 `.venv`、`build`、spec 文件、缓存和临时报告等过程文件。

## 项目结构

```text
dirdiff-master/
  app.py                  # GUI 主程序，包含全部核心逻辑
  assets/                 # 图标资源
    app.ico               # 应用图标
  scripts/                # 构建脚本
    build_exe.ps1         # Windows 构建脚本
  .gitignore
  LICENSE
  pyproject.toml
  README.md
  requirements.txt
```
