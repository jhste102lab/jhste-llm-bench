// Leaderboard data. Values are aggregated from private local experiment artifacts (~/.omp/experiments/*); only summaries live here.
// Shape: roles[] -> suites[] -> scopes{worker|chain} -> {metrics, rank, rows}. Metric labels come from i18n `metric.<key>`.
// kinds: ratio [a,b] | pct | usd | dur (seconds) | tokens | count. better: high | low. `harness` defaults to the top-level value; rows may override.
(() => {
  const M = {
    pass: { key: "pass", kind: "ratio", better: "high", bar: true, primary: true },
    first: { key: "first", kind: "ratio", better: "high", bar: true, primary: true },
    recall: { key: "recall", kind: "ratio", better: "high", bar: true, primary: true },
    acc: { key: "acc", kind: "pct", better: "high", bar: true, primary: true },
    found: { key: "found", kind: "count", better: "high", bar: true, primary: true },
    cost: { key: "cost", kind: "usd", better: "low", bar: true, perTask: true },
    time: { key: "time", kind: "dur", better: "low", bar: true, perTask: true },
    span: { key: "time", kind: "dur", better: "low", bar: true },
    req: { key: "req", kind: "count", better: "low", perTask: true },
    wrong: { key: "wrong", kind: "count", better: "low" },
    icost: { key: "icost", kind: "usd", better: "low", bar: true },
    qcost: { key: "qcost", kind: "usd", better: "low", bar: true },
    planted: { key: "planted", kind: "ratio", better: "high" },
    tokens: { key: "tokens", kind: "tokens", better: "low", bar: true },
    jobs: { key: "jobs", kind: "count", better: "high" },
    fcost: { key: "fcost", kind: "usd", better: "low" },
    ftime: { key: "ftime", kind: "dur", better: "low" },
    rework: { key: "rework", kind: "pct", better: "low" },
    defect: { key: "defect", kind: "pct", better: "low" },
  };
  const r3 = (x) => Math.round(x * 10000) / 10000;

  window.BENCH = {
    harness: "omp",
    vendors: {
      anthropic: { label: "Anthropic", color: "#d97757" },
      openai: { label: "OpenAI", color: "#10a37f" },
      deepseek: { label: "DeepSeek", color: "#4d6bfe" },
      engine: { label: "검색 API", color: "#8e897d" },
    },
    models: {
      ds: { name: "DeepSeek V4.1 Flash 1017", short: "DeepSeek", vendor: "deepseek" },
      sol: { name: "GPT-6.1 Sol", short: "Sol", vendor: "openai" },
      luna: { name: "GPT-6 Luna", short: "Luna", vendor: "openai" },
      sonnet: { name: "Claude Sonnet 5.5", short: "Sonnet", vendor: "anthropic" },
      opus: { name: "Claude Opus 5.5", short: "Opus", vendor: "anthropic" },
      haiku: { name: "Claude Haiku 5.5", short: "Haiku", vendor: "anthropic" },
      exa: { name: "Exa", short: "Exa", vendor: "engine" },
      brave: { name: "Brave Search", short: "Brave", vendor: "engine" },
      tinyfish: { name: "TinyFish", short: "TinyFish", vendor: "engine" },
      parallel: { name: "Parallel", short: "Parallel", vendor: "engine" },
    },
    roles: [
      {
        id: "task",
        suites: [
          {
            id: "coding", label: { ko: "코딩 과제" }, protocol: "v1", date: "2026.10.20", runs: 15,
            tasks: [
              { id: "c1", label: { ko: "집계 정책 변경" } },
              { id: "c2", label: { ko: "권한 경계·감사 로그" } },
              { id: "c3", label: { ko: "중복 목록 통합" } },
            ],
            scopes: {
              worker: {
                metrics: [M.pass, M.cost, M.time], rank: ["pass", "cost"], scatter: { x: "cost", y: "pass" },
                taskKey: "cases",
                rows: [
                  { model: "ds", effort: "high", n: 1, values: { pass: [3, 3], cost: r3(0.1543 / 3), time: 1823 / 3 },
                    tasks: { c1: { cases: [9, 9], cost: 0.0873, time: 1083 }, c2: { cases: [6, 6], cost: 0.0536, time: 546 }, c3: { cases: [28, 28], cost: 0.0134, time: 194 } } },
                  { model: "ds", effort: "max", n: 1, values: { pass: [3, 3], cost: r3(0.1856 / 3), time: 1631 / 3 },
                    tasks: { c1: { cases: [9, 9], cost: 0.1276, time: 921 }, c2: { cases: [6, 6], cost: 0.0446, time: 515 }, c3: { cases: [28, 28], cost: 0.0134, time: 195 } } },
                  { model: "sol", effort: "high", n: 1, values: { pass: [3, 3], cost: r3(1.3093 / 3), time: 826 / 3 },
                    tasks: { c1: { cases: [9, 9], cost: 0.6913, time: 328 }, c2: { cases: [6, 6], cost: 0.4207, time: 276 }, c3: { cases: [28, 28], cost: 0.1973, time: 222 } } },
                  { model: "sonnet", effort: "high", n: 1, values: { pass: [3, 3], cost: r3(2.5696 / 3), time: 628 / 3 },
                    tasks: { c1: { cases: [9, 9], cost: 1.7657, time: 445 }, c2: { cases: [6, 6], cost: 0.5541, time: 117 }, c3: { cases: [28, 28], cost: 0.2498, time: 66 } } },
                  { model: "opus", effort: "medium", n: 1, values: { pass: [3, 3], cost: r3(5.1435 / 3), time: 978 / 3 },
                    tasks: { c1: { cases: [9, 9], cost: 2.6357, time: 620 }, c2: { cases: [6, 6], cost: 1.7737, time: 248 }, c3: { cases: [28, 28], cost: 0.7341, time: 110 } } },
                ],
              },
              chain: {
                metrics: [M.pass, M.first, M.cost, M.time], rank: ["pass", "first", "cost"], scatter: { x: "cost", y: "first" },
                taskKey: "first",
                rows: [
                  { model: "ds", effort: "max", n: 1, values: { pass: [3, 3], first: [3, 3], cost: r3(3.607692 / 3), time: (2364 + 750.6 + 347.5) / 3 },
                    tasks: { c1: { first: true, cost: 1.759, time: 2364 }, c2: { first: true, cost: 1.2403, time: 750.6 }, c3: { first: true, cost: 0.6084, time: 347.5 } } },
                  { model: "ds", effort: "high", n: 1, values: { pass: [3, 3], first: [3, 3], cost: r3(3.748714 / 3), time: (2299.8 + 1036.9 + 371.6) / 3 },
                    tasks: { c1: { first: true, cost: 2.1313, time: 2299.8 }, c2: { first: true, cost: 0.9514, time: 1036.9 }, c3: { first: true, cost: 0.666, time: 371.6 } } },
                  { model: "sol", effort: "high", n: 1, values: { pass: [3, 3], first: [3, 3], cost: r3(4.920885 / 3), time: (757.5 + 568.5 + 365) / 3 },
                    tasks: { c1: { first: true, cost: 2.7004, time: 757.5 }, c2: { first: true, cost: 1.4295, time: 568.5 }, c3: { first: true, cost: 0.791, time: 365 } } },
                  { model: "sonnet", effort: "high", n: 1, values: { pass: [3, 3], first: [3, 3], cost: r3(6.014482 / 3), time: (832 + 358.3 + 230.2) / 3 },
                    tasks: { c1: { first: true, cost: 3.4227, time: 832 }, c2: { first: true, cost: 1.7112, time: 358.3 }, c3: { first: true, cost: 0.8806, time: 230.2 } } },
                  { model: "opus", effort: "medium", n: 1, values: { pass: [3, 3], first: [2, 3], cost: r3(11.324764 / 3), time: (1350.5 + 472.7 + 266.8) / 3 },
                    tasks: { c1: { first: false, cost: 6.8636, time: 1350.5 }, c2: { first: true, cost: 3.1386, time: 472.7 }, c3: { first: true, cost: 1.3225, time: 266.8 } } },
                ],
              },
            },
          },
          {
            id: "confirm", label: { ko: "재확인" }, protocol: "v1", date: "2026.10.20", runs: 4,
            tasks: [
              { id: "c4", label: { ko: "표시 조건 수정" } },
              { id: "c1", label: { ko: "집계 정책 변경" } },
            ],
            scopes: {
              worker: {
                metrics: [M.pass, M.cost, M.time], rank: ["pass", "cost"], scatter: { x: "cost", y: "pass" },
                taskKey: "cases",
                rows: [
                  { model: "ds", effort: "max", n: 1, values: { pass: [2, 2], cost: r3((0.0136 + 0.1103) / 2), time: (125 + 892) / 2 },
                    tasks: { c4: { cases: [18, 18], cost: 0.0136, time: 125 }, c1: { cases: [9, 9], cost: 0.1103, time: 892 } } },
                  { model: "sol", effort: "high", n: 1, values: { pass: [2, 2], cost: r3((0.1429 + 0.6876) / 2), time: (88 + 279) / 2 },
                    tasks: { c4: { cases: [18, 18], cost: 0.1429, time: 88 }, c1: { cases: [9, 9], cost: 0.6876, time: 279 } } },
                ],
              },
              chain: {
                metrics: [M.pass, M.first, M.cost, M.time], rank: ["pass", "first", "cost"], scatter: { x: "cost", y: "first" },
                taskKey: "first",
                rows: [
                  { model: "ds", effort: "max", n: 1, values: { pass: [2, 2], first: [2, 2], cost: r3(2.190449 / 2), time: (252.4 + 1635.2) / 2 },
                    tasks: { c4: { first: true, cost: 0.4886, time: 252.4 }, c1: { first: true, cost: 1.7019, time: 1635.2 } } },
                  { model: "sol", effort: "high", n: 1, values: { pass: [2, 2], first: [2, 2], cost: r3(2.854959 / 2), time: (247 + 633) / 2 },
                    tasks: { c4: { first: true, cost: 0.7156, time: 247 }, c1: { first: true, cost: 2.1394, time: 633 } } },
                ],
              },
            },
          },
        ],
        field: {
          period: { ko: "09.23 – 10.19" },
          metrics: [M.jobs, M.fcost, M.ftime, M.rework, M.defect],
          rows: [
            { model: "opus", effort: "medium", values: { jobs: 190, fcost: 2.48, ftime: 486, rework: 14, defect: 4.1 } },
            { model: "sonnet", effort: "high", values: { jobs: 541, fcost: 1.92, ftime: 642, rework: 28, defect: 8.7 } },
          ],
        },
      },
      {
        id: "scout",
        suites: [
          {
            id: "explore", label: { ko: "탐색 과제" }, protocol: "v1", date: "2026.10.20", runs: 15,
            tasks: [
              { id: "s1", label: { ko: "결과 그룹핑" } },
              { id: "s2", label: { ko: "이름 정규화" } },
              { id: "s3", label: { ko: "텍스트 노이즈 제거" } },
            ],
            scopes: {
              worker: {
                metrics: [M.recall, M.cost, M.time, M.req], rank: ["recall", "cost"], scatter: { x: "cost", y: "recall" },
                taskKey: "recall",
                rows: [
                  { model: "ds", effort: "max", n: 1, values: { recall: [11, 15], cost: r3(0.2044 / 3), time: 2664 / 3, req: 49 },
                    tasks: { s1: { recall: [1, 1], cost: 0.0603, time: 923 }, s2: { recall: [3, 5], cost: 0.0814, time: 1055 }, s3: { recall: [7, 9], cost: 0.0627, time: 686 } } },
                  { model: "ds", effort: "high", n: 1, values: { recall: [11, 15], cost: r3(0.2739 / 3), time: 2727 / 3, req: 59 },
                    tasks: { s1: { recall: [1, 1], cost: 0.0648, time: 854 }, s2: { recall: [4, 5], cost: 0.0984, time: 858 }, s3: { recall: [6, 9], cost: 0.1107, time: 1015 } } },
                  { model: "sonnet", effort: "medium", n: 1, values: { recall: [11, 15], cost: r3(1.4748 / 3), time: 232 / 3, req: 12 },
                    tasks: { s1: { recall: [1, 1], cost: 0.6182, time: 79 }, s2: { recall: [3, 5], cost: 0.5559, time: 85 }, s3: { recall: [7, 9], cost: 0.3007, time: 68 } } },
                  { model: "sol", effort: "medium", n: 1, values: { recall: [10, 15], cost: r3(0.8093 / 3), time: 439 / 3, req: 10 },
                    tasks: { s1: { recall: [1, 1], cost: 0.2793, time: 151 }, s2: { recall: [3, 5], cost: 0.2825, time: 146 }, s3: { recall: [6, 9], cost: 0.2475, time: 142 } } },
                  { model: "sol", effort: "high", n: 1, values: { recall: [10, 15], cost: r3(1.1306 / 3), time: 631 / 3, req: 12 },
                    tasks: { s1: { recall: [1, 1], cost: 0.4116, time: 222 }, s2: { recall: [3, 5], cost: 0.3761, time: 234 }, s3: { recall: [6, 9], cost: 0.3429, time: 175 } } },
                ],
              },
              chain: {
                metrics: [M.pass, M.cost, M.time], rank: ["pass", "cost"], scatter: { x: "cost", y: "pass" },
                taskKey: "checks",
                rows: [
                  { model: "sol", effort: "high", n: 1, values: { pass: [3, 3], cost: r3(3.92 / 3), time: 808 },
                    tasks: { s1: { checks: [8, 8], cost: 0.947, time: 632 }, s2: { checks: [11, 11], cost: 0.917, time: 568 }, s3: { checks: [22, 22], cost: 2.057, time: 1225 } } },
                  { model: "ds", effort: "high", n: 1, values: { pass: [2, 3], cost: r3(1.93 / 3), time: 1278 },
                    tasks: { s1: { checks: [8, 8], cost: 0.377, time: 1068 }, s2: { checks: [11, 11], cost: 0.597, time: 1158 }, s3: { checks: [21, 22], cost: 0.956, time: 1609 } } },
                  { model: "ds", effort: "max", n: 1, values: { pass: [2, 3], cost: r3(2.737 / 3), time: 1378 },
                    tasks: { s1: { checks: [8, 8], cost: 1.09, time: 1404 }, s2: { checks: [11, 11], cost: 0.522, time: 1382 }, s3: { checks: [21, 22], cost: 1.125, time: 1348 } } },
                  { model: "sonnet", effort: "medium", n: 1, values: { pass: [2, 3], cost: r3(3.873 / 3), time: 788 },
                    tasks: { s1: { checks: [8, 8], cost: 1.327, time: 1018 }, s2: { checks: [11, 11], cost: 1.027, time: 443 }, s3: { checks: [21, 22], cost: 1.519, time: 905 } } },
                  { model: "sol", effort: "medium", n: 1, values: { pass: [2, 3], cost: r3(3.982 / 3), time: 782 },
                    tasks: { s1: { checks: [8, 8], cost: 1.221, time: 758 }, s2: { checks: [11, 11], cost: 0.926, time: 503 }, s3: { checks: [21, 22], cost: 1.835, time: 1086 } } },
                ],
              },
            },
          },
        ],
        field: {
          period: { ko: "10.03 – 10.19" },
          metrics: [M.jobs, M.fcost, M.ftime],
          rows: [
            { model: "sol", effort: "medium", values: { jobs: 283, fcost: 0.34, ftime: 174 } },
            { model: "sonnet", effort: "medium", values: { jobs: 455, fcost: 1.07, ftime: 162 } },
            { model: "opus", effort: "medium", values: { jobs: 16, fcost: 2.12, ftime: 198 } },
          ],
        },
      },
      {
        id: "bulk",
        suites: [
          {
            id: "hard", label: { ko: "고난도 추적" }, protocol: "v1", date: "2026.10.15", runs: 91, taskCount: 29, unit: "item",
            tasks: [
              { id: "deps", label: { ko: "역의존 추적" } },
              { id: "calls", label: { ko: "호출 위치 세기" } },
              { id: "schema", label: { ko: "최종 스키마" } },
            ],
            scopes: {
              worker: {
                metrics: [M.acc, M.icost, M.span], rank: ["acc", "icost"], scatter: { x: "icost", y: "acc" },
                taskKey: "acc",
                rows: [
                  { model: "haiku", effort: "xhigh", n: 3, values: { acc: 89.7, icost: 0.061, time: 568, wrong: 8 }, tasks: { deps: { acc: 93 }, calls: { acc: 82 }, schema: { acc: 100 } } },
                  { model: "haiku", effort: "high", n: 3, values: { acc: 79.3, icost: 0.04, time: 529, wrong: 16 }, tasks: { deps: { acc: 73 }, calls: { acc: 74 }, schema: { acc: 100 } } },
                  { model: "haiku", effort: "medium", n: 1, values: { acc: 75.9, icost: 0.019, time: 479, wrong: 3 }, tasks: { deps: { acc: 60 }, calls: { acc: 77 }, schema: { acc: 100 } } },
                  { model: "luna", effort: "xhigh", n: 3, values: { acc: 70.1, icost: 0.005, time: 532, wrong: 21 }, tasks: { deps: { acc: 63 }, calls: { acc: 67 }, schema: { acc: 89 } } },
                  { model: "luna", effort: "high", n: 3, values: { acc: 36.8, icost: 0.004, time: 488, wrong: 49 }, tasks: { deps: { acc: 7 }, calls: { acc: 51 }, schema: { acc: 56 } } },
                ],
              },
            },
          },
          {
            id: "registry", label: { ko: "레지스트리" }, protocol: "v1", date: "2026.10.14", runs: 210, taskCount: 100, unit: "item", tasks: [],
            scopes: {
              worker: {
                metrics: [M.acc, M.icost, M.span, M.wrong], rank: ["acc", "icost"], scatter: { x: "icost", y: "acc" },
                rows: [
                  { model: "haiku", effort: "xhigh", n: 2, ci: { acc: [94, 99] }, values: { acc: 97.0, icost: 0.0655, time: 322, wrong: 2 } },
                  { model: "luna", effort: "xhigh", n: 2, ci: { acc: [93, 99] }, values: { acc: 96.0, icost: 0.0088, time: 373, wrong: 5 } },
                  { model: "luna", effort: "max", n: 2, ci: { acc: [92, 99] }, values: { acc: 95.3, icost: 0.0106, time: 471, wrong: 1 } },
                  { model: "haiku", effort: "medium", n: 2, ci: { acc: [91, 98] }, values: { acc: 94.7, icost: 0.0435, time: 195, wrong: 3 } },
                  { model: "haiku", effort: "high", n: 2, ci: { acc: [85, 94] }, values: { acc: 89.7, icost: 0.0514, time: 255, wrong: 7 } },
                  { model: "luna", effort: "high", n: 2, ci: { acc: [83, 93] }, values: { acc: 88.7, icost: 0.0058, time: 206, wrong: 12 } },
                  { model: "luna", effort: "medium", n: 2, ci: { acc: [47, 63] }, values: { acc: 54.7, icost: 0.0025, time: 128, wrong: 20 } },
                ],
              },
            },
          },
          {
            id: "code", label: { ko: "코드 지식" }, protocol: "v1", date: "2026.10.15", runs: 200, taskCount: 75, unit: "item", tasks: [],
            scopes: {
              worker: {
                metrics: [M.acc, M.icost, M.span, M.wrong], rank: ["acc", "icost"], scatter: { x: "icost", y: "acc" },
                rows: [
                  { model: "haiku", effort: "xhigh", n: 2, ci: { acc: [94, 98] }, values: { acc: 95.9, icost: 0.01, time: 100, wrong: 0 } },
                  { model: "haiku", effort: "high", n: 2, values: { acc: 95.8, icost: 0.009, time: 60, wrong: 2 } },
                  { model: "haiku", effort: "medium", n: 2, values: { acc: 94.6, icost: 0.006, time: 50, wrong: 1 } },
                  { model: "luna", effort: "xhigh", n: 2, values: { acc: 92.1, icost: 0.002, time: 61, wrong: 10 } },
                  { model: "luna", effort: "high", n: 2, values: { acc: 89.0, icost: 0.001, time: 43, wrong: 17 } },
                ],
              },
            },
          },
        ],
      },
      {
        id: "web",
        suites: [
          {
            id: "search", label: { ko: "웹검색" }, protocol: "v1", date: "2026.10.14", runs: 1350, taskCount: 75, unit: "item", tasks: [],
            scopes: {
              worker: {
                metrics: [M.acc, M.qcost], rank: ["acc"], scatter: { x: "qcost", y: "acc" },
                rows: [
                  { model: "luna", effort: null, n: 3, ci: { acc: [90.9, 97.6] }, values: { acc: 94.4, qcost: 0.0007 } },
                  { model: "exa", effort: null, n: 3, values: { acc: 85.8, qcost: 0.014 } },
                  { model: "brave", effort: null, n: 3, values: { acc: 84.0, qcost: null } },
                  { model: "tinyfish", effort: null, n: 3, values: { acc: 76.7, qcost: null } },
                  { model: "parallel", effort: null, n: 3, values: { acc: 74.0, qcost: null } },
                  { model: "haiku", effort: null, n: 3, ci: { acc: [49.3, 68.9] }, values: { acc: 59.3, qcost: 0.02 } },
                ],
              },
            },
          },
        ],
      },
      {
        id: "review",
        suites: [
          {
            id: "pr", label: { ko: "PR 리뷰" }, protocol: "v1", date: "2026.10.15", runs: 8, taskCount: 4, tasks: [], archived: true,
            scopes: {
              worker: {
                metrics: [M.found, M.planted, M.span, M.tokens], rank: ["found", "time"],
                rows: [
                  { model: "luna", effort: "xhigh", n: 1, values: { found: 2, planted: [0, 3], time: 599, tokens: 3.2e6 } },
                  { model: "haiku", effort: "xhigh", n: 1, values: { found: 0, planted: [0, 3], time: 1876, tokens: 14.7e6 } },
                ],
              },
            },
          },
        ],
      },
    ],
  };
})();
