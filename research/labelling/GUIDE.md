# Labelling guide for the judge-reliability measurement (E1)

You are the ground truth. Each item shows a customer **question**, the **reference answer** and an
**answer** written by a model. Score how correct the answer is, from 1 to 5, using the reference as
the truth. The judge model sees exactly what you see (question, reference answer, answer).

| Score | Meaning |
| --- | --- |
| 5 | Every fact in the reference is stated correctly, and nothing contradicts it. |
| 4 | Correct, but misses one part of the reference (a condition, a number, a yes/no detail). |
| 3 | About half right: some of the reference is there, some missing or vague. |
| 2 | Mostly wrong, or contradicts the reference on an important point. |
| 1 | Wrong, off topic, or a refusal to answer. |

Rules, decided before you start (keep them the same for all 100 items):

1. **Length earns nothing.** A one-sentence answer that has every fact is a 5. A long answer that
   misses one is a 4.
2. **Extra detail that does not contradict the reference is ignored.** It neither helps nor hurts.
   If it contradicts the reference, the answer is at most a 2.
3. **Judge the facts, not the wording.** "30 days" and "thirty days from delivery" are the same.
4. **When two scores seem right, take the lower one.**
5. **Do not look at any judge score** while labelling, and do not go back and change earlier labels
   to make them agree with later ones. If a rule is missing, add it here and apply it from then on.

Label in one or two sittings (about an hour for 100). Number keys score, Enter submits.
