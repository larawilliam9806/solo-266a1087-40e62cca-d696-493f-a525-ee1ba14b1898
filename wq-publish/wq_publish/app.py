"""Flask 路由：上传校验 → 预览增删改 → 确认整批替换；串号核查 → 更名建议 → 确认置换。"""
from __future__ import annotations

import os
import secrets
import sqlite3

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .storage import LIVE_FILENAME, PublishConflictError, Storage
from .swapcheck import (
    MAX_SELECTED,
    MIN_COMMON_ANALYTES,
    MIN_SELECTED,
    changed_pairs,
    common_analytes,
    cost_matrix,
    evidence_rows,
    identity_cost,
    mapping_from_assignment,
    optimal_assignments,
    possible_targets,
)
from .validation import format_concentration, validate_csv

MAX_UPLOAD_BYTES = 16 * 1024 * 1024


def create_app(data_dir: str | None = None, downstream_dir: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    base = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    data_dir = data_dir or os.environ.get("WQ_DATA_DIR") or os.path.join(base, "data")
    downstream_dir = (
        downstream_dir
        or os.environ.get("WQ_DOWNSTREAM_DIR")
        or os.path.join(base, "downstream")
    )

    secret_path = os.path.join(data_dir, "flask_secret")
    os.makedirs(data_dir, exist_ok=True)
    if os.path.exists(secret_path):
        with open(secret_path, "rb") as fh:
            app.secret_key = fh.read()
    else:
        key = secrets.token_bytes(32)
        with open(secret_path, "wb") as fh:
            fh.write(key)
        os.chmod(secret_path, 0o600)
        app.secret_key = key

    storage = Storage(data_dir, downstream_dir)
    app.storage = storage  # 供自测脚本直接访问

    @app.template_filter("conc")
    def _conc(value):
        return format_concentration(value)

    @app.get("/")
    def index():
        reg = storage.current_registry()
        rows = storage.current_rows()
        events = storage.recent_events()
        return render_template(
            "index.html", reg=reg, rows=rows, events=events,
            live_filename=LIVE_FILENAME,
        )

    @app.post("/preview")
    def preview():
        upload = request.files.get("csv_file")
        batch_no = request.form.get("batch_no", "")
        if upload is None or not upload.filename:
            flash("请选择仪器导出的 CSV 文件", "error")
            return redirect(url_for("index"))
        raw = upload.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            flash("CSV 文件超过 16MB 上限", "error")
            return redirect(url_for("index"))

        rows, errors = validate_csv(raw, batch_no)
        if errors:
            for msg in errors:
                flash(msg, "error")
            return redirect(url_for("index"))

        reg = storage.current_registry()
        base_version = reg["current_version"] if reg else 0
        token = storage.create_session(batch_no.strip(), base_version, rows)
        diff = storage.preview_diff(rows)
        storage.log(
            "preview",
            f"会话 {token[:8]} 基于 v{base_version} 预览批次 {batch_no.strip()}："
            f"新增 {len(diff['inserts'])} / 修改 {len(diff['updates'])} / "
            f"撤回 {len(diff['deletes'])} / 不变 {len(diff['unchanged'])}",
        )
        return render_template(
            "preview.html",
            token=token,
            batch_no=batch_no.strip(),
            base_version=base_version,
            current_version=base_version,
            rows=rows,
            diff=diff,
            live_filename=LIVE_FILENAME,
        )

    @app.post("/publish/<token>")
    def publish(token):
        session = storage.get_session(token)
        if session is None:
            flash("修订会话不存在或已过期，请重新上传 CSV", "error")
            return redirect(url_for("index"))

        reg = storage.current_registry()
        current_version = reg["current_version"] if reg else 0
        if session["status"] != "pending" or session["base_version"] != current_version:
            # 会话在预览期间被别的发布超越：作废会话，展示基于旧版的差异，要求重新预览
            if session["status"] == "pending":
                storage.mark_obsolete(token, current_version)
                stale_reason = (
                    f"您基于 v{session['base_version']} 发起修订，"
                    f"但当前版本已变为 v{current_version}（另一页面已成功发布）"
                )
            else:
                reason_map = {
                    "published": "该修订已经发布，不能重复发布",
                    "obsolete": "该预览已被更新的发布超越而作废",
                    "cancelled": "该修订已被放弃（取消）",
                }
                stale_reason = reason_map.get(session["status"], "该修订会话已失效")
            diff = storage.preview_diff(session["rows"])
            storage.log(
                "conflict",
                f"会话 {token[:8]} 发布被拒（基线 v{session['base_version']}，"
                f"当前 v{current_version}，状态 {session['status']}）",
            )
            return render_template(
                "preview.html",
                token=token,
                batch_no=session["batch_no"],
                base_version=session["base_version"],
                current_version=current_version,
                rows=session["rows"],
                diff=diff,
                stale=True,
                stale_reason=stale_reason,
                live_filename=LIVE_FILENAME,
            ), 409

        try:
            new_version, row_count = storage.publish(token)
        except PublishConflictError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        except sqlite3.OperationalError as exc:  # 极端并发下写锁等待超时
            app.logger.warning("publish lock contention: %s", exc)
            flash("当前有其他发布正在写入，请稍后重新预览再发布", "error")
            return redirect(url_for("index"))
        flash(f"发布成功：v{new_version} 已生效，共 {row_count} 行", "success")
        return redirect(url_for("index"))

    @app.post("/cancel/<token>")
    def cancel(token):
        if storage.cancel_session(token):
            flash("已放弃本次修订，当前发布版本未受影响", "info")
        return redirect(url_for("index"))

    # ---------- 串号核查 ----------
    @app.get("/swap-check")
    def swap_check():
        reg = storage.current_registry()
        versions = storage.list_versions()
        known = {v["version"] for v in versions}
        ref_version = request.args.get("ref", type=int)
        cur_version = request.args.get("cur", type=int)
        if cur_version is None and reg:
            cur_version = reg["current_version"]

        common_samples = None
        if ref_version is not None and cur_version is not None:
            if ref_version == cur_version:
                flash("参考版本与当前版本不能相同", "error")
            elif ref_version not in known or cur_version not in known:
                flash("所选版本不存在", "error")
            else:
                ref_stats = {
                    s["sample_no"]: s["analyte_count"]
                    for s in storage.version_samples(ref_version)
                }
                cur_stats = {
                    s["sample_no"]: s["analyte_count"]
                    for s in storage.version_samples(cur_version)
                }
                common_samples = [
                    {
                        "sample_no": s,
                        "ref_count": ref_stats[s],
                        "cur_count": cur_stats[s],
                    }
                    for s in sorted(ref_stats.keys() & cur_stats.keys())
                ]
        return render_template(
            "swap_check.html",
            reg=reg,
            versions=versions,
            ref_version=ref_version,
            cur_version=cur_version,
            common_samples=common_samples,
            min_selected=MIN_SELECTED,
            max_selected=MAX_SELECTED,
        )

    @app.post("/swap-check/analyze")
    def swap_analyze():
        versions = {v["version"]: v for v in storage.list_versions()}
        try:
            ref_version = int(request.form.get("ref", ""))
            cur_version = int(request.form.get("cur", ""))
        except (TypeError, ValueError):
            flash("版本选择无效", "error")
            return redirect(url_for("swap_check"))
        if (ref_version == cur_version or ref_version not in versions
                or cur_version not in versions):
            flash("参考版本与当前版本必须不同且都是已发布版本", "error")
            return redirect(url_for("swap_check"))

        selected = list(dict.fromkeys(request.form.getlist("sample")))
        ref_rows = storage.version_rows(ref_version)
        cur_rows = storage.version_rows(cur_version)
        both = ({r["sample_no"] for r in ref_rows}
                & {r["sample_no"] for r in cur_rows})
        if not (MIN_SELECTED <= len(selected) <= MAX_SELECTED):
            flash(f"请圈选 {MIN_SELECTED}～{MAX_SELECTED} 个两版共有的样本号", "error")
            return redirect(url_for("swap_check", ref=ref_version, cur=cur_version))
        if any(s not in both for s in selected):
            flash("只能圈选两版共有的样本号", "error")
            return redirect(url_for("swap_check", ref=ref_version, cur=cur_version))

        base_ctx = dict(
            ref_version=ref_version,
            cur_version=cur_version,
            ref_batch=versions[ref_version]["batch_no"],
            cur_batch=versions[cur_version]["batch_no"],
            selected=selected,
        )
        common_items, per_sample = common_analytes(ref_rows, cur_rows, selected)
        if len(common_items) < MIN_COMMON_ANALYTES:
            storage.log(
                "swap_check",
                f"串号核查：参考 v{ref_version} × 当前 v{cur_version}，圈选 "
                f"{len(selected)} 个样本，共有检测项目不足 {MIN_COMMON_ANALYTES} 个"
                f"（{'、'.join(common_items) or '无'}），缺项，未给出更名建议",
            )
            return render_template(
                "swap_result.html", outcome="missing",
                common_items=common_items, per_sample=per_sample, **base_ctx,
            )

        costs = cost_matrix(ref_rows, cur_rows, selected, common_items)
        best, optima = optimal_assignments(costs)
        ident = identity_cost(costs)

        if len(optima) > 1:
            storage.log(
                "swap_check",
                f"串号核查：参考 v{ref_version} × 当前 v{cur_version}，存在 "
                f"{len(optima)} 个并列最优方案（最低总代价 {best:.6g}），"
                "不允许确认更名",
            )
            return render_template(
                "swap_result.html", outcome="multiple",
                common_items=common_items, best=best, optima_count=len(optima),
                possibilities=possible_targets(selected, optima), **base_ctx,
            )

        if list(optima[0]) == list(range(len(selected))):
            storage.log(
                "swap_check",
                f"串号核查：参考 v{ref_version} × 当前 v{cur_version}，"
                f"同名配对即为最优（总代价 {ident:.6g}），未发现串号迹象",
            )
            return render_template(
                "swap_result.html", outcome="no_swap",
                common_items=common_items, ident=ident, **base_ctx,
            )

        # 唯一最优且不同于同名配对 ⇒ 必然严格优于同名配对，给出更名建议
        mapping = mapping_from_assignment(selected, optima[0])
        reg = storage.current_registry()
        latest = reg["current_version"] if reg else 0
        if cur_version != latest:
            return render_template(
                "swap_result.html", outcome="stale",
                common_items=common_items,
                stale_reason=(
                    f"核查基于的当前版 v{cur_version} 已非最新登记版本 "
                    f"v{latest}，更名建议不可确认"
                ),
                **base_ctx,
            )

        changed = changed_pairs(mapping)
        token = storage.create_swap_session(
            ref_version, cur_version, versions[cur_version]["batch_no"], mapping,
        )
        storage.log(
            "swap_check",
            f"串号核查：参考 v{ref_version} × 当前 v{cur_version}，唯一最优且优于"
            f"同名配对（{best:.6g} < {ident:.6g}），给出更名建议 "
            + "、".join(f"{k}→{v}" for k, v in sorted(changed.items())),
        )
        return render_template(
            "swap_result.html", outcome="suggest",
            common_items=common_items, best=best, ident=ident,
            mapping=sorted(changed.items()),
            kept=[s for s in selected if s not in changed],
            evidence=evidence_rows(ref_rows, cur_rows, mapping, common_items),
            token=token, **base_ctx,
        )

    @app.post("/swap-check/confirm/<token>")
    def swap_confirm(token):
        session = storage.get_swap_session(token)
        if session is None:
            flash("串号更名建议不存在或已过期，请重新核查", "error")
            return redirect(url_for("swap_check"))

        def stale_page(reason):
            return render_template(
                "swap_result.html", outcome="stale",
                ref_version=session["ref_version"],
                cur_version=session["cur_version"],
                ref_batch="",
                cur_batch=session["batch_no"],
                selected=sorted(session["mapping"].keys()),
                common_items=None,
                stale_reason=reason,
            ), 409

        reg = storage.current_registry()
        current_version = reg["current_version"] if reg else 0
        if session["status"] != "pending" or session["cur_version"] != current_version:
            # 确认前当前版已被他人发布的新版本替换：旧建议失效，原文件和登记不变
            if session["status"] == "pending":
                storage.mark_swap_obsolete(token, current_version)
                reason = (
                    f"建议已失效：核查基于当前版 v{session['cur_version']}，"
                    f"但当前版本已变为 v{current_version}（他人已发布新版本）。"
                    "原文件和登记未做任何更改，请重新核查。"
                )
            else:
                reason = {
                    "published": "该更名建议已经发布，不能重复确认",
                    "obsolete": "该更名建议已被更新的发布超越而作废",
                    "cancelled": "该更名建议已被放弃（取消）",
                }.get(session["status"], "该更名建议已失效")
            return stale_page(reason)

        try:
            new_version, row_count, changed = storage.publish_swap(token)
        except PublishConflictError as exc:
            # 预检后、事务前又被其他发布超越的极端竞态
            return stale_page(str(exc))
        except sqlite3.OperationalError as exc:  # 极端并发下写锁等待超时
            app.logger.warning("swap publish lock contention: %s", exc)
            flash("当前有其他发布正在写入，请稍后重试", "error")
            return redirect(url_for("swap_check"))
        pairs = "、".join(f"{k}→{v}" for k, v in sorted(changed.items()))
        flash(
            f"串号更名已发布：v{new_version}（依据参考 v{session['ref_version']} × "
            f"当前 v{session['cur_version']} 的核查建议），置换样本号 {pairs}，"
            f"浓度与其他行未变，共 {row_count} 行",
            "success",
        )
        return redirect(url_for("index"))

    @app.post("/swap-check/cancel/<token>")
    def swap_cancel(token):
        if storage.cancel_swap_session(token):
            flash("已放弃本次串号更名建议，当前发布版本未受影响", "info")
        return redirect(url_for("swap_check"))

    @app.get("/published.csv")
    def published_csv():
        if not os.path.exists(storage.live_path):
            abort(404, description="尚无已发布的 CSV")
        return send_file(
            storage.live_path, mimetype="text/csv",
            download_name=LIVE_FILENAME, as_attachment=False,
        )

    @app.errorhandler(413)
    def too_large(_):
        flash("上传文件超过 16MB 上限", "error")
        return redirect(url_for("index"))

    return app
