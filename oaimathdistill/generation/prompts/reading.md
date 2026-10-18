Generate mathematical reading comprehension from the supplied local content.
For form=qa (default), instruction asks a concrete question about definitions,
notation, assumptions, a logical relationship, or why a stated step follows;
answer gives a grounded explanation with necessary mathematical detail.
For form=explanation, instruction asks for a connected explanation of the local
passage; answer connects its concepts, intuition, and argument in coherent prose.
These are two forms of one reading method. Do not duplicate the source excerpt
in instruction; the runner supplies it to the student. Explain unfamiliar
notation only when its meaning is supported. Distinguish supplied facts from
background explanations. Return empty if no worthwhile supported example exists.
