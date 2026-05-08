"""
占位入口：若 IDE/书签仍指向本文件，会打开本页。
印章抠图请用 SealCut，并优先用固定端口启动脚本，避免与 8501 上其它 Streamlit 混淆。
"""

import streamlit as st

st.set_page_config(page_title="占位说明", layout="centered")
st.error("这是 simli 项目的占位页，不是 SealCut。")
st.title("你很可能连错了端口或旧进程")
st.markdown(
    """
浏览器若一直是 **localhost:8501**，上面可能仍是别的 Streamlit；你再开一个 SealCut 时，往往会跑到 **8502、8503…**，地址栏不换就会一直看到本页。

**请按下面做（推荐）：**

1. 在终端执行（会固定用 **8765** 端口）：

```bash
bash /Users/apple/SealCut/run_streamlit.sh
```

2. 浏览器打开终端里提示的地址：**http://127.0.0.1:8765**

3. 若仍异常，把其它 Streamlit 终端里 **Ctrl+C** 停掉，再重新执行上一步。

---

若坚持用默认端口：

```bash
cd /Users/apple/SealCut && source .venv/bin/activate && streamlit run "$(pwd)/app.py"
```

务必看终端打印的 **Local URL** 里的端口号。

在线版：[SealCut Streamlit](https://sealcut.streamlit.app/)
"""
)
