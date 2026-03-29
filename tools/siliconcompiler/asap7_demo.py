# Import necessary classes from the siliconcompiler framework and the LambdaPDK.
from siliconcompiler import ASIC
from siliconcompiler.flows import asicflow, synflow

from lambdapdk.asap7.libs.asap7sc7p5t import ASAP7SC7p5RVT, ASAP7SC7p5SLVT, ASAP7SC7p5LVT
from lambdapdk.asap7.libs.fakeram7 import FakeRAM7Lambdalib_SinglePort, \
                                          FakeRAM7Lambdalib_DoublePort, \
                                          FakeRAM7Lambdalib_TrueDoublePort
from lambdapdk.asap7.libs.fakeio7 import FakeIO7Lambdalib_IO


####################################################
# Target Setup Function
####################################################
def asap7_demo(
        project: ASIC,
        syn_np: int = 1,
        floorplan_np: int = 1, place_np: int = 1, cts_np: int = 1, route_np: int = 1,
        timing_np: int = 1):

    project.set_mainlib(ASAP7SC7p5RVT())
    project.add_asiclib(ASAP7SC7p5LVT())
    project.add_asiclib(ASAP7SC7p5SLVT())

    project.set_flow(asicflow.ASICFlow(
        syn_np=syn_np,
        floorplan_np=floorplan_np,
        place_np=place_np,
        cts_np=cts_np,
        route_np=route_np))
    project.add_dep(synflow.SynthesisFlow(
        syn_np=syn_np,
        timing_np=timing_np))

    project.set_pdk("asap7")

    # # Slow corner - setup timing
    # scenario_slow = project.constraint.timing.make_scenario("slow")
    # scenario_slow.add_libcorner(["slow", "generic"])
    # scenario_slow.set_pexcorner("slow")

    # # Fast corner - hold timing
    # scenario_fast = project.constraint.timing.make_scenario("fast")
    # scenario_fast.add_libcorner(["fast", "generic"])
    # scenario_fast.set_pexcorner("fast")

    # Typical corner - power
    scenario_typ = project.constraint.timing.make_scenario("typical")
    scenario_typ.add_libcorner(["typical", "generic"])
    scenario_typ.set_pexcorner("typical")
    scenario_typ.add_check("power")
    scenario_typ.add_check("setup")
    scenario_typ.add_check("hold")



    project.set_asic_delaymodel("nldm")

    area = project.constraint.area
    area.set_density(50)
    area.set_coremargin(1)

    FakeRAM7Lambdalib_SinglePort.alias(project)
    FakeRAM7Lambdalib_DoublePort.alias(project)
    FakeRAM7Lambdalib_TrueDoublePort.alias(project)
    FakeIO7Lambdalib_IO.alias(project)