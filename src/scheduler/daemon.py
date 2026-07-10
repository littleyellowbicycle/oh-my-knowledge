"""APScheduler 内嵌守护进程 — 随 kb serve 启动，后台定时执行任务。

用法:
    from src.scheduler.daemon import start_scheduler, stop_scheduler
    start_scheduler()   # 启动后台调度
    stop_scheduler()    # 关闭
"""

from __future__ import annotations

import logging

from src.scheduler import config, tasks

logger = logging.getLogger(__name__)

_scheduler = None


def _daily_job():
    """APScheduler 定时回调: 执行 run_daily()。"""
    try:
        results = tasks.run_daily()
        for r in results:
            status = "OK" if r.success else f"FAIL: {r.error}"
            logger.info("Scheduler | %s | %d entries | %.1fs | %s",
                        r.name, r.entries, r.duration, status)
    except Exception as e:  # noqa: BLE001
        logger.error("Scheduler daily job 异常: %s", e, exc_info=True)


def start_scheduler() -> bool:
    """启动后台调度器，每天定时执行 run_daily()。

    返回 True 表示成功启动，False 表示 APScheduler 未安装或已启动。
    """
    global _scheduler
    if _scheduler is not None:
        logger.warning("Scheduler 已在运行，跳过重复启动")
        return False

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler 未安装，定时调度未启用 (pip install apscheduler)")
        return False

    _scheduler = BackgroundScheduler(timezone=config.TIMEZONE)
    _scheduler.add_job(
        _daily_job,
        CronTrigger(hour=config.DAILY_HOUR, minute=config.DAILY_MINUTE),
        id="daily_pipeline",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler 已启动: daily_pipeline @ %02d:%02d %s",
                config.DAILY_HOUR, config.DAILY_MINUTE, config.TIMEZONE)
    return True


def stop_scheduler() -> None:
    """关闭调度器。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler 已停止")


def get_scheduler_info() -> dict:
    """返回当前调度器状态（供 CLI / API 查询）。"""
    if _scheduler is None:
        return {"running": False, "jobs": []}
    jobs = []
    for job in _scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
            "trigger": str(job.trigger),
        })
    return {"running": _scheduler.running, "jobs": jobs}
