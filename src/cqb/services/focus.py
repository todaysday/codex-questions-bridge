"""The focus question of a consultant topic (design section 2.3): what the owner's words most likely refer to.

Set when the owner replies to a card, writes a Q-number or presses a card button; kept in the consultant row, so it
survives a restart. It moves on by itself: a focus whose question is no longer pending is ignored, and the newest
pending question takes over (marked `assumed` when several are pending). The consultant sees it as the context line."""

from cqb.dao.consultants import ConsultantDao
from cqb.domain.labels import mentioned_numbers
from cqb.domain.models import AgentQuestion, Question
from cqb.services.agent_questions import AgentQuestionService
from cqb.services.question_context import context_line
from cqb.services.questions import QuestionService


class QuestionFocus:
    def __init__(
        self, consultants: ConsultantDao, questions: QuestionService, agent_questions: AgentQuestionService,
        source_id: str,
    ) -> None:
        self._consultants = consultants
        self._questions = questions
        self._agent_questions = agent_questions
        self._source = source_id

    def point(self, root_thread_id: str, number: int) -> bool:
        """The owner pointed at `Q<number>` of this thread; only a pending question can become the focus."""
        consultant = self._consultants.get(self._source, root_thread_id)
        if consultant is None or not any(q.display_no == number for q in self._pending(root_thread_id)):
            return False
        if consultant.focus_no != number:
            self._consultants.set_focus(consultant.consultant_id, number)
        return True

    def point_card(self, card: Question | AgentQuestion) -> bool:
        return self.point(card.root_thread_id, card.display_no)

    def note_mentions(self, root_thread_id: str, text: str) -> None:
        """The first Q-number of the text that names a pending question of this thread becomes the focus."""
        for number in mentioned_numbers(text):
            if self.point(root_thread_id, number):
                return

    def line(self, root_thread_id: str) -> str:
        """The context line for the consultant; empty when nothing is pending."""
        consultant = self._consultants.get(self._source, root_thread_id)
        return context_line(self._pending(root_thread_id), consultant.focus_no if consultant else None)

    __call__ = line

    def _pending(self, root_thread_id: str) -> list[Question | AgentQuestion]:
        return [*self._questions.open_questions(root_thread_id), *self._agent_questions.open_questions(root_thread_id)]
