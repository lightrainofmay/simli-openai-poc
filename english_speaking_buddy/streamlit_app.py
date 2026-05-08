"""Placeholder Streamlit page (main product is the LiveKit realtime web on port 8030)."""

import streamlit as st

st.set_page_config(page_title="英语口语语伴", layout="centered")
st.title("英语口语语伴")
st.info(
    "实时数字人请使用 **FastAPI + LiveKit** 页面，而不是本 Streamlit 占位页。\n\n"
    "在仓库根目录执行 `./start_all.sh` 后打开：http://127.0.0.1:8030\n\n"
    "详见 `README.md`。"
)
