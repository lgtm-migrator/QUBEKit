"""
An interface to charge mol via gaussian.
"""

import os
import shutil
import subprocess as sp
from typing import Any, Dict, Optional

from jinja2 import Template
from pydantic import Field
from qcelemental.util import which
from typing_extensions import Literal

from qubekit.charges.base import ChargeBase
from qubekit.charges.solvent_settings import SolventGaussian
from qubekit.charges.utils import ExtractChargeData
from qubekit.engines import GaussianHarness, call_qcengine
from qubekit.molecules import Ligand
from qubekit.utils.datastructures import LocalResource, QCOptions
from qubekit.utils.exceptions import ChargemolError
from qubekit.utils.file_handling import folder_setup, get_data


class DDECCharges(ChargeBase):

    type: Literal["DDECCharges"] = "DDECCharges"
    program: Literal["gaussian"] = "gaussian"
    ddec_version: Literal[3, 6] = Field(
        6, description="The version of DDEC partitioning that should be used."
    )
    solvent_settings: Optional[SolventGaussian] = Field(
        SolventGaussian(),
        description="The engine that should be used to generate the reference density to perform the AIM analysis on.",
    )

    def start_message(self, **kwargs) -> str:
        return f"Calculating charges using chargemol and ddec{self.ddec_version}."

    @classmethod
    def is_available(cls) -> bool:
        """
        Check that chargemol and gaussian can be found.
        """
        gaussian = GaussianHarness.found()
        if not gaussian:
            raise RuntimeError(
                "Gaussian 09/16 was not found please make sure they are available."
            )

        chargemol = which(
            "chargemol",
            return_bool=True,
            raise_error=True,
            raise_msg="Please install chargemol via `conda install chargemol -c conda-forge`.",
        )
        return gaussian and chargemol

    def _build_chargemol_input(
        self, density_file_name: str, molecule: "Ligand"
    ) -> None:
        """
        Build the input control file for a chargemol job using the reference template.
        """
        # get the chargemol template data
        template_file = get_data(os.path.join("templates", "chargemol.txt"))
        with open(template_file) as file:
            template = Template(file.read())

        chargemol_path = shutil.which("chargemol")
        # now split to find the chargemol atomic densities
        chargemol_path = chargemol_path.split("bin")[0]
        chargemol_path = os.path.join(
            chargemol_path,
            "share",
            "chargemol",
        )

        # gather the required template data
        template_data = dict(
            charge=molecule.charge,
            ddec_version=self.ddec_version,
            density_file=density_file_name,
            chargemol_dir=chargemol_path,
        )

        rendered_template = template.render(**template_data)
        # write the job control file
        with open("job_control.txt", "w") as job_file:
            job_file.write(rendered_template)
        return

    def _call_chargemol(
        self, density_file_content: str, molecule: "Ligand", cores: int
    ) -> "Ligand":
        """
        Run ChargeMol on the density file from gaussian and extract the AIM reference data and store it into the molecule.

        Args:
            density_file_content: A string containing the density file content which will be wrote to file.
            molecule: The molecule the reference data should be stored into.

        Returns:
            A molecule updated with the ChargeMol reference data.
        """
        with folder_setup(folder_name=f"ChargeMol_{molecule.name}"):
            # write the wfx file
            density_file = f"{molecule.name}.wfx"
            with open(density_file, "w+") as d_file:
                d_file.write(density_file_content)

            # build the chargemol input
            self._build_chargemol_input(
                density_file_name=density_file, molecule=molecule
            )

            # Export a variable to the environment that chargemol will use to work out the threads, must be a string
            os.environ["OMP_NUM_THREADS"] = str(cores)
            with open("log.txt", "w+") as log:

                try:
                    sp.run(
                        "chargemol job_controll.txt",
                        shell=True,
                        stdout=log,
                        stderr=log,
                        check=True,
                    )
                    return ExtractChargeData.extract_charge_data_chargemol(
                        molecule=molecule, dir_path="", ddec_version=self.ddec_version
                    )

                except sp.CalledProcessError:
                    raise ChargemolError(
                        "Chargemol did not execute properly; check the output file for details."
                    )
                finally:
                    del os.environ["OMP_NUM_THREADS"]

    def _gas_calculation_settings(self) -> Dict[str, Any]:
        extras = dict(
            cmdline_extra=[
                "density=current",
                "OUTPUT=WFX",
            ],
            add_input=["", "gaussian.wfx"],
        )
        return extras

    def _execute(
        self, molecule: "Ligand", local_options: LocalResource, qc_spec: QCOptions
    ) -> "Ligand":
        """
        Generate a electron density using gaussian and partition using DDEC before storing back into the molecule.

        Note:
            The current coordinates are used for the calculation.

        Args:
            molecule: The molecule we want to run the calculation on.

        Returns:
            The molecule updated with the raw partitioned reference data.
        """
        if qc_spec.td_settings is not None:
            # we need to set the solvent solver to PCM
            self.solvent_settings.solver_type = "PCM"
        # get the solvent keywords
        extras = self._get_calculation_settings()

        result = call_qcengine(
            molecule=molecule,
            driver="energy",
            qc_spec=qc_spec,
            local_options=local_options,
            extras=extras,
        )
        return self._call_chargemol(
            density_file_content=result.extras["gaussian.wfx"],
            molecule=molecule,
            cores=local_options.cores,
        )
