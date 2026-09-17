"""Research package: data capture, cost modelling, execution simulation
and strategy evaluation for the Lead Engine.

It is SEPARATE from `t3_engine.lead_engine` on purpose and the dependency
runs one way. The live engine does not import anything from here, so a
half-finished experiment cannot change what the live engine does. This
package reads the engine's message stream through an explicit tap and
otherwise keeps to itself.
"""
