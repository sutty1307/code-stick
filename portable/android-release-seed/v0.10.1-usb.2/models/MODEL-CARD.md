
---

library_name: transformers
license: llama3.2
base_model: meta-llama/Llama-3.2-3B-Instruct
tags:
- abliterated
- uncensored

---

[![QuantFactory Banner](https://lh7-rt.googleusercontent.com/docsz/AD_4nXeiuCm7c8lEwEJuRey9kiVZsRn2W-b4pWlu3-X534V3YmVuVc2ZL-NXg2RkzSOOS2JXGHutDuyyNAUtdJI65jGTo8jT9Y99tMi4H4MqL44Uc5QKG77B0d6-JfIkZHFaUA71-RtjyYZWVIhqsNZcx8-OMaA?key=xt3VSDoCbmTY7o-cwwOFwQ)](https://hf.co/QuantFactory)


# QuantFactory/Llama-3.2-3B-Instruct-abliterated-GGUF
This is quantized version of [huihui-ai/Llama-3.2-3B-Instruct-abliterated](https://huggingface.co/huihui-ai/Llama-3.2-3B-Instruct-abliterated) created using llama.cpp

# Original Model Card


# 🦙 Llama-3.2-3B-Instruct-abliterated



This is an uncensored version of Llama 3.2 3B Instruct created with abliteration (see [this article](https://huggingface.co/blog/mlabonne/abliteration) to know more about it).

Special thanks to [@FailSpy](https://huggingface.co/failspy) for the original code and technique. Please follow him if you're interested in abliterated models.

## Evaluations
The following data has been re-evaluated and calculated as the average for each test.

| Benchmark   | Llama-3.2-3B-Instruct | Llama-3.2-3B-Instruct-abliterated |
|-------------|-----------------------|-----------------------------------|
| IF_Eval     | 76.55                 | **76.76**                         |
| MMLU Pro    | 27.88                 | **28.00**                         |
| TruthfulQA  | 50.55                 | **50.73**                         |
| BBH         | 41.81                 | **41.86**                         |
| GPQA        | 28.39                 | **28.41**                         |

The script used for evaluation can be found inside this repository under /eval.sh, or click [here](https://huggingface.co/huihui-ai/Llama-3.2-3B-Instruct-abliterated/blob/main/eval.sh)

