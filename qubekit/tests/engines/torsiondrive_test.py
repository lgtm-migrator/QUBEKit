"""
Test the torsiondrive json api interface.
"""
from typing import Any, Dict

import numpy as np
import pytest
from torsiondrive import td_api

from qubekit.engines import TorsionDriver, optimise_grid_point
from qubekit.molecules import Ligand
from qubekit.utils import constants
from qubekit.utils.datastructures import (
    LocalResource,
    QCOptions,
    TDSettings,
    TorsionScan,
)
from qubekit.utils.file_handling import get_data


@pytest.fixture
def ethane_state(tmpdir) -> Dict[str, Any]:
    """
    build an initial state for a ethane scan.
    """
    with tmpdir.as_cwd():
        mol = Ligand.from_file(get_data("ethane.sdf"))
        bond = mol.find_rotatable_bonds()[0]
        dihedral = mol.dihedrals[bond.indices][0]
        tdriver = TorsionDriver(grid_spacing=15)
        # make the scan data
        dihedral_data = TorsionScan(torsion=dihedral, scan_range=(-165, 180))
        qc_spec = QCOptions(program="rdkit", basis=None, method="uff")
        td_state = tdriver._create_initial_state(
            molecule=mol, dihedral_data=dihedral_data, qc_spec=qc_spec
        )
        return td_state


@pytest.mark.parametrize(
    "starting_conformations", [pytest.param(1, id="1"), pytest.param(4, id="4")]
)
def test_get_initial_state(tmpdir, starting_conformations):
    """
    Make sure we can correctly build a starting state using the torsiondrive api.
    """
    with tmpdir.as_cwd():
        mol = Ligand.from_file(get_data("ethane.sdf"))
        bond = mol.find_rotatable_bonds()[0]
        dihedral = mol.dihedrals[bond.indices][0]
        tdriver = TorsionDriver(starting_conformations=starting_conformations)
        # make the scan data
        dihedral_data = TorsionScan(torsion=dihedral, scan_range=(-165, 180))
        td_state = tdriver._create_initial_state(
            molecule=mol, dihedral_data=dihedral_data, qc_spec=QCOptions()
        )
        assert td_state["dihedrals"] == [
            dihedral,
        ]
        assert td_state["elements"] == [atom.atomic_symbol for atom in mol.atoms]
        assert td_state["dihedral_ranges"] == [
            (-165, 180),
        ]
        assert np.allclose(
            (mol.coordinates * constants.ANGS_TO_BOHR), td_state["init_coords"][0]
        )
        # make sure we have tried to generate conformers
        assert len(td_state["init_coords"]) <= tdriver.starting_conformations


def test_initial_state_coords_passed(tmpdir):
    """
    Make sure any seed conformations are used in the initial state
    """
    with tmpdir.as_cwd():
        mol = Ligand.from_file(get_data("ethane.sdf"))
        bond = mol.find_rotatable_bonds()[0]
        dihedral = mol.dihedrals[bond.indices][0]
        tdriver = TorsionDriver()
        # make the scan data
        dihedral_data = TorsionScan(torsion=dihedral, scan_range=(-165, 180))
        # make some mock coords
        coords = [np.random.random(size=(mol.n_atoms, 3)) for _ in range(4)]
        td_state = tdriver._create_initial_state(
            molecule=mol,
            dihedral_data=dihedral_data,
            qc_spec=QCOptions(),
            seed_coordinates=coords,
        )
        assert len(td_state["init_coords"]) == 4
        # make sure they are the same random coords
        for i in range(4):
            assert np.allclose(
                (coords[i] * constants.ANGS_TO_BOHR), td_state["init_coords"][i]
            )


def test_optimise_grid_point_and_update(tmpdir, ethane_state):
    """
    Try and perform a single grid point optimisation.
    """
    with tmpdir.as_cwd():
        mol = Ligand.from_file(get_data("ethane.sdf"))
        tdriver = TorsionDriver(n_workers=1)
        qc_spec = QCOptions(program="rdkit", basis=None, method="uff")
        local_ops = LocalResource(cores=1, memory=1)
        geo_opt = tdriver._build_geometry_optimiser()
        # get the job inputs
        new_jobs = tdriver._get_new_jobs(td_state=ethane_state)
        coords = new_jobs["-60"][0]
        result = optimise_grid_point(
            geometry_optimiser=geo_opt,
            qc_spec=qc_spec,
            local_options=local_ops,
            molecule=mol,
            coordinates=coords,
            dihedral=ethane_state["dihedrals"][0],
            dihedral_angle=-60,
            job_id=0,
        )
        new_state = tdriver._update_state(
            td_state=ethane_state,
            result_data=[
                result,
            ],
        )
        next_jobs = tdriver._get_new_jobs(td_state=new_state)
        assert "-75" in next_jobs
        assert "-45" in next_jobs


@pytest.mark.parametrize(
    "workers", [pytest.param(1, id="1 worker"), pytest.param(2, id="2 workers")]
)
def test_full_tdrive(tmpdir, workers, capsys):
    """
    Try and run a full torsiondrive for ethane with a cheap rdkit method.
    """
    with tmpdir.as_cwd():

        ethane = Ligand.from_file(get_data("ethane.sdf"))
        # make the scan data
        bond = ethane.find_rotatable_bonds()[0]
        dihedral = ethane.dihedrals[bond.indices][0]
        dihedral_data = TorsionScan(torsion=dihedral, scan_range=(-165, 180))
        qc_spec = QCOptions(program="rdkit", basis=None, method="uff")
        local_ops = LocalResource(cores=workers, memory=2)
        tdriver = TorsionDriver(
            n_workers=workers,
            grid_spacing=60,
        )
        _ = tdriver.run_torsiondrive(
            molecule=ethane,
            dihedral_data=dihedral_data,
            qc_spec=qc_spec,
            local_options=local_ops,
        )
        captured = capsys.readouterr()
        # make sure a fresh torsiondrive is run
        assert "Starting new torsiondrive" in captured.out


@pytest.mark.parametrize(
    "qc_options, scan_range, compatible",
    [
        pytest.param(
            QCOptions(program="rdkit", method="uff", basis=None, td_settings=None),
            (-165, 180),
            True,
            id="Compatible",
        ),
        pytest.param(
            QCOptions(program="xtb", method="gfn2xtb", basis=None, td_settings=None),
            (-165, 180),
            False,
            id="Wrong program",
        ),
        pytest.param(
            QCOptions(
                program="rdkit",
                method="uff",
                basis=None,
                td_settings=TDSettings(n_states=3),
            ),
            (-165, 180),
            False,
            id="TD settings",
        ),
        pytest.param(
            QCOptions(program="rdkit", method="uff", basis=None),
            (0, 180),
            False,
            id="Wrong torsion range",
        ),
    ],
)
def test_load_old_state(tmpdir, ethane_state, qc_options, scan_range, compatible):
    """
    Make sure we can load and cross-check torsiondrive state files.
    """
    with tmpdir.as_cwd():
        # dump the basic ethane result to file
        td_api.current_state_json_dump(
            current_state=ethane_state, jsonfilename="torsiondrive_state.json"
        )
        td = TorsionDriver()
        state = td._load_state(
            qc_spec=qc_options,
            torsion_scan=TorsionScan(
                ethane_state["dihedrals"][0], scan_range=scan_range
            ),
        )
        if compatible:
            assert state is not None
        else:
            assert state is None


def test_tdrive_restarts(capsys, ethane_state, tmpdir):
    """
    Make sure that an old torsiondrive is continued when possible from the current state file.
    """
    with tmpdir.as_cwd():
        ethane_state["grid_spacing"] = [
            60,
        ]
        mol = Ligand.from_file(get_data("ethane.sdf"))
        tdriver = TorsionDriver(n_workers=1, grid_spacing=60)
        qc_spec = QCOptions(program="rdkit", basis=None, method="uff")
        local_ops = LocalResource(cores=1, memory=1)
        geo_opt = tdriver._build_geometry_optimiser()
        # get the job inputs
        new_jobs = tdriver._get_new_jobs(td_state=ethane_state)
        coords = new_jobs["-60"][0]
        result = optimise_grid_point(
            geometry_optimiser=geo_opt,
            qc_spec=qc_spec,
            local_options=local_ops,
            molecule=mol,
            coordinates=coords,
            dihedral=ethane_state["dihedrals"][0],
            dihedral_angle=-60,
            job_id=0,
        )
        _ = tdriver._update_state(
            td_state=ethane_state,
            result_data=[
                result,
            ],
        )
        # now start a run and make sure it continues
        _ = tdriver.run_torsiondrive(
            molecule=mol,
            dihedral_data=TorsionScan(
                torsion=ethane_state["dihedrals"][0], scan_range=(-165, 180)
            ),
            qc_spec=qc_spec,
            local_options=local_ops,
        )
        capture = capsys.readouterr()
        assert (
            "Compatible TorsionDrive state found restarting torsiondrive!"
            in capture.out
        )


def test_get_new_jobs(ethane_state):
    """
    Make sure that for a given initial state we can get the next jobs to be done.
    """
    tdriver = TorsionDriver()
    new_jobs = tdriver._get_new_jobs(td_state=ethane_state)
    assert "-60" in new_jobs
    assert new_jobs["-60"][0] == pytest.approx(
        [
            -1.44942051524959,
            0.015117815022160003,
            -0.030235630044320005,
            1.44942051524959,
            -0.015117815022160003,
            0.030235630044320005,
            -2.2431058039129903,
            -0.18897268777700002,
            1.8708296089923,
            -2.16562700192442,
            1.78768162637042,
            -0.82203119182995,
            -2.1920831782132,
            -1.5325684978714702,
            -1.18863820611733,
            2.1920831782132,
            1.5306787709937002,
            1.18863820611733,
            2.2431058039129903,
            0.18897268777700002,
            -1.8708296089923,
            2.16562700192442,
            -1.78957135324819,
            0.82014146495218,
        ]
    )
