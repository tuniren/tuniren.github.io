---
title: 技术博客的第一篇文章
description: 建立一个以工程判断为核心的技术博客，并说明文章记录的基本结构。
pubDate: 2026-09-04
tags:
  - engineering
  - blogging
  - astro
---

## 目标

技术博客的价值不只在于记录结论，更在于保存推理过程。一个可复用的技术记录通常至少包含四个部分：

1. 上下文：问题在什么系统、团队或约束中出现。
2. 约束：时间、成本、兼容性、可靠性和维护性的边界。
3. 取舍：为什么选择某一方案，并放弃其他方案。
4. 证据：构建结果、测试结果、线上指标或复盘数据。

## 技术选择

对于程序员个人站点，静态生成是较稳妥的默认选择。它降低运行时复杂度，把主要风险转移到构建阶段；Markdown 或 MDX 则让文章内容可以随着代码一起版本化。

## 数据结构

```ts
type EngineeringNote = {
  context: string;
  constraints: string[];
  decision: string;
  evidence: string[];
};
```

## 后续维护

这个站点使用 Astro 内容集合管理文章元数据。后续新增文章时，只需要在 `src/content/blog` 下添加 Markdown 文件即可。
