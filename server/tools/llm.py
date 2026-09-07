"""LLM-инструменты: chat (с квотой и cost-логом) + личный отчёт студента."""
from __future__ import annotations

from ..auth import current_student
from ..clients import chat as chat_client
from ..db import QuotaExceeded, check_quota, log_usage, student_report
from ..pricing import cost_rub


def register(mcp) -> None:
    @mcp.tool
    async def chat(
        prompt: str,
        session_id: str,
        profile: str = "standard",
        system: str = "",
        model: str = "",
        prompt_version: str = "",
        max_tokens: int = 4096,
    ) -> dict:
        """Спросить LLM через курсовой шлюз. Единственная точка доступа к моделям.

        Args:
            prompt: запрос пользователя.
            session_id: id рабочей сессии (лимит 5M ток/сессия, 5 сессий/неделю).
            profile: 'code' (Qwen3.8-27B) | 'research' (только DeepSeek) | 'standard'.
            system: системный промпт (опционально).
            model: явно зафиксировать модель, минуя каскад (опционально).
            prompt_version: версия промпта для A/B и cost-журнала.
            max_tokens: потолок ответа.

        Returns:
            {text, model, input_tokens, output_tokens, cost_rub, quota}.
        """
        student = current_student()
        try:
            await check_quota(student.db_id, session_id)
        except QuotaExceeded as e:
            return {"error": "quota_exceeded", "message": str(e)}

        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        res = await chat_client(
            messages,
            profile=profile,
            model=model or None,
            max_tokens=max_tokens,
        )
        cost = cost_rub(res["model"], res["input_tokens"], res["output_tokens"])
        await log_usage(
            student_id=student.db_id,
            username=student.username,
            session_id=session_id,
            kind="llm",
            model=res["model"],
            profile=profile,
            input_tokens=res["input_tokens"],
            output_tokens=res["output_tokens"],
            cost_rub=cost,
            prompt_version=prompt_version or None,
        )
        quota = await student_report(student.db_id, session_id)
        return {
            "text": res["text"],
            "model": res["model"],
            "input_tokens": res["input_tokens"],
            "output_tokens": res["output_tokens"],
            "finish_reason": res.get("finish_reason", ""),
            "truncated": res.get("finish_reason") == "length",
            "cost_rub": cost,
            "quota": quota,
        }

    @mcp.tool
    async def my_usage(session_id: str = "") -> dict:
        """Мой расход: за неделю, по сессиям и по текущей сессии (для личного кабинета)."""
        student = current_student()
        return await student_report(student.db_id, session_id or None)
