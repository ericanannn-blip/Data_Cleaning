# 不接大模型的分类方法

本轮只调查方法，没有实现分类器、执行真实文件分类或调用外部模型。当前网站的主题/任务结果仍可能为“证据不足”。

## 可行的规则方案

对能够提取正文的文件，可采用：正文解析 → 主题词典/明确模式 → 排除与边界条件 → 候选标签和命中原文 → 人工确认。项目可以用普通 JavaScript 实现，不必安装 spaCy 或连接模型。

现有12个主题、15个任务可保持为固定代码。`include/exclude/boundary` 目前是自然语言规则说明，后续需要逐项转换为可执行、可审核的条件，不能直接视为已实现分类器。每个候选应保存规则ID和版本、来源SHA-256、命中位置及理由，保持待复核；无命中或冲突不能自动归入“其他”。

“任务”表示用户要做的动作，而不是文档类型。长文章不等于用户要总结，含代码不等于用户要编程；应依据明确指令或用户选择的用途，否则留空。

## 覆盖限制

- 文本、JSON、CSV和原生文字PDF适合先建立规则基线；实际仍需正文读取与清洗，不能只看扩展名。
- 图片或扫描PDF需要先识字。严格不使用任何模型时，应由人工处理；本地Tesseract不调用外部API，但仍使用训练得到的识别模型，不能叫“完全无模型”。
- TF-IDF可帮助检索相似规则，不会自动建立正确的主题语义。
- 规则方案适合明确、重复出现的用语，无法承诺复杂语义的准确率。后续应先用人工留出样本测漏分、误分和待复核比例，不以JSON校验通过代替分类准确性。
- 文件正文证据继续使用独立文件分析契约，不改变原聊天schema的用户文本与附件边界。

## 依据

- [spaCy：规则匹配适用于明确模式和有限词表](https://spacy.io/usage/rule-based-matching)
- [pypdf：文字提取与扫描页限制](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)
- [Tesseract：本地OCR、LSTM及训练数据](https://github.com/tesseract-ocr/tesseract)
- [scikit-learn：文字特征与TF-IDF](https://scikit-learn.org/stable/modules/feature_extraction.html#text-feature-extraction)
