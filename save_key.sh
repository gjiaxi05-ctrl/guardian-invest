#!/bin/zsh
# 从剪贴板读取 DeepSeek API key，存进 macOS 钥匙串，然后清空剪贴板。key 不会显示在屏幕上。
echo "1) 去 DeepSeek 后台复制你的 API key（以 sk- 开头）"
echo "2) 复制好以后，回到这里直接按回车（不要粘贴）"
read -rs _
key="$(pbpaste | tr -d '[:space:]')"
if [[ "$key" != sk-* ]]; then
  echo "剪贴板里的内容不像 DeepSeek key（应以 sk- 开头）。请重新复制后再运行：zsh save_key.sh"
  exit 1
fi
security add-generic-password -U -s guardian-deepseek -a guardian -w "$key" \
  && echo "OK: key 已存入钥匙串（长度 ${#key}，结尾 ...${key[-4,-1]}）"
printf '' | pbcopy
echo "剪贴板已清空。"
