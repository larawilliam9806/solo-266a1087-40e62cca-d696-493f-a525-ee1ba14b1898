"""串号核查核心算法：共有项目组、配对代价、全局最优一对一指派、更名计划。

业务背景：检测批次疑似贴错样本号。选定历史参考版本与当前版本、圈选
2～8 个两版共有的样本号后，按「各检测项目浓度绝对差之和」计算当前样本号
与参考样本号两两配对的代价，枚举全排列求全局一对一最小总代价指派：

* 所选样本在两版必须有同一组至少两个检测项目，否则属「缺项」，不给建议；
* 存在多个并列最优方案时，逐样本列出其在所有最优方案中可能对应的编号，
  不允许确认更名；
* 唯一最优且优于同名配对时，给出当前号 → 参考号的更名建议。

圈选上限 8 个样本：8! = 40320 种指派，枚举足够快且能完整列出并列最优。
"""
from __future__ import annotations

from itertools import permutations

MIN_SELECTED = 2
MAX_SELECTED = 8
MIN_COMMON_ANALYTES = 2

# 浮点总代价并列判定容差（相对）：把因浮点求和顺序造成的尾差归并为同一最优
_TOL = 1e-9


def analytes_by_sample(rows: list[dict]) -> dict[str, set[str]]:
    """{样本号: {检测项目, ...}}"""
    result: dict[str, set[str]] = {}
    for r in rows:
        result.setdefault(r["sample_no"], set()).add(r["analyte"])
    return result


def common_analytes(ref_rows: list[dict], cur_rows: list[dict],
                    selected: list[str]):
    """返回 (共有项目组, 每样本项目对照)。

    共有项目组 = 所有圈选样本在两版各自检测项目集合的总交集，用于配对代价
    计算；不足 MIN_COMMON_ANALYTES 个时调用方按「缺项」处理。
    per_sample 供缺项提示：每个样本在两版的项目清单及各自缺了哪些项。
    """
    ref_map = analytes_by_sample(ref_rows)
    cur_map = analytes_by_sample(cur_rows)
    common: set[str] | None = None
    per_sample: list[dict] = []
    for sample_no in selected:
        ref_items = ref_map.get(sample_no, set())
        cur_items = cur_map.get(sample_no, set())
        inter = ref_items & cur_items
        common = inter if common is None else common & inter
        per_sample.append({
            "sample_no": sample_no,
            "ref_analytes": sorted(ref_items),
            "cur_analytes": sorted(cur_items),
            "missing_in_cur": sorted(ref_items - cur_items),
            "missing_in_ref": sorted(cur_items - ref_items),
        })
    return sorted(common or set()), per_sample


def _concentration_index(rows: list[dict]) -> dict[tuple[str, str], float]:
    return {(r["sample_no"], r["analyte"]): r["concentration"] for r in rows}


def cost_matrix(ref_rows: list[dict], cur_rows: list[dict],
                selected: list[str], analytes: list[str]) -> list[list[float]]:
    """costs[i][j] = 当前版 selected[i] 与参考版 selected[j] 在共有项目上
    的浓度绝对差之和。"""
    ref_conc = _concentration_index(ref_rows)
    cur_conc = _concentration_index(cur_rows)
    matrix: list[list[float]] = []
    for cur_no in selected:
        row = []
        for ref_no in selected:
            row.append(sum(
                abs(cur_conc[(cur_no, a)] - ref_conc[(ref_no, a)])
                for a in analytes
            ))
        matrix.append(row)
    return matrix


def identity_cost(costs: list[list[float]]) -> float:
    """同名配对（当前号 i ↔ 参考号 i）的总代价。"""
    return sum(costs[i][i] for i in range(len(costs)))


def optimal_assignments(costs: list[list[float]]):
    """枚举全排列求全局一对一最小总代价指派。

    返回 (最低总代价, [最优排列, ...])；排列 perm[i]=j 表示当前第 i 个
    样本应对应参考第 j 个样本。并列最优全部列出，供调用方判定是否允许
    确认更名。
    """
    n = len(costs)
    best: float | None = None
    best_perms: list[tuple[int, ...]] = []
    for perm in permutations(range(n)):
        total = 0.0
        for i in range(n):
            total += costs[i][perm[i]]
        if best is None or total < best - _TOL * max(1.0, abs(best)):
            best = total
            best_perms = [perm]
        elif abs(total - best) <= _TOL * max(1.0, abs(best)):
            best_perms.append(perm)
    return best, best_perms


def possible_targets(selected: list[str],
                     optima: list[tuple[int, ...]]) -> list[dict]:
    """多最优时：当前每个样本在所有最优方案中可能对应的参考编号。"""
    return [
        {
            "cur_no": selected[i],
            "candidates": sorted({selected[perm[i]] for perm in optima}),
        }
        for i in range(len(selected))
    ]


def mapping_from_assignment(selected: list[str],
                            perm: tuple[int, ...]) -> dict[str, str]:
    """最优指派 → {当前号: 参考号} 全量映射（含同名不变项）。

    映射是圈选样本号集合上的一个置换，因此按它改写样本号不会与未圈选
    样本冲突，也不会破坏 (样本号, 检测项目) 唯一性。
    """
    return {selected[i]: selected[perm[i]] for i in range(len(selected))}


def changed_pairs(mapping: dict[str, str]) -> dict[str, str]:
    """从全量映射中筛出实际发生更名的 当前号→参考号。"""
    return {cur: ref for cur, ref in mapping.items() if cur != ref}


def evidence_rows(ref_rows: list[dict], cur_rows: list[dict],
                  mapping: dict[str, str], analytes: list[str]) -> list[dict]:
    """更名建议的逐项对照证据：当前号浓度 vs 建议对应参考号浓度。"""
    ref_conc = _concentration_index(ref_rows)
    cur_conc = _concentration_index(cur_rows)
    rows: list[dict] = []
    for cur_no, ref_no in sorted(changed_pairs(mapping).items()):
        for analyte in analytes:
            cur_value = cur_conc[(cur_no, analyte)]
            ref_value = ref_conc[(ref_no, analyte)]
            rows.append({
                "cur_no": cur_no,
                "ref_no": ref_no,
                "analyte": analyte,
                "cur_conc": cur_value,
                "ref_conc": ref_value,
                "diff": abs(cur_value - ref_value),
            })
    return rows


def apply_mapping(rows: list[dict], mapping: dict[str, str]) -> list[dict]:
    """按更名映射置换样本号；浓度与其他行保持原样。"""
    return [
        {
            "sample_no": mapping.get(r["sample_no"], r["sample_no"]),
            "analyte": r["analyte"],
            "concentration": r["concentration"],
        }
        for r in rows
    ]
