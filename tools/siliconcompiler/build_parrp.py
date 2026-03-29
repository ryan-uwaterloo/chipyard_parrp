from pathlib import Path
from siliconcompiler import ASIC, Design
from asap7_demo import asap7_demo
from lambdalib.ramlib import Dpram, Spram


def build_chip():
    # ------------------------------------------
    # Design object
    # ------------------------------------------
    design = Design("ParRP")

    rtl_dir = Path("src/parrp")

    # Add all SystemVerilog files
    for sv in rtl_dir.glob("*.sv"):
        design.add_file(str(sv), fileset="rtl")

    # Let lambdalib resolve the correct implementation per tool stage
    design.add_depfileset(Dpram(), depfileset='rtl', fileset='rtl')
    design.add_depfileset(Spram(), depfileset='rtl', fileset='rtl')

    # Set top module
    # design.set_topmodule("MulticoreTraceTileHarness", fileset="rtl")
    design.set_topmodule("InclusiveCache", fileset="rtl")
    
    design.set('fileset', 'rtl', 'define', 'SYNTHESIS=TRUE')

    design.add_file("constraints.sdc", fileset="sdc")

    # ------------------------------------------
    # ASIC project
    # ------------------------------------------
    chip = ASIC(design)

    # Enable RTL fileset
    chip.add_fileset(["rtl", "sdc"])

    # Apply ASAP7 target
    asap7_demo(chip)

    chip.option.set('scheduler', 'maxthreads', 4)

    return chip


def main():
    chip = build_chip()

    chip.run()
    chip.summary()


if __name__ == "__main__":
    main()