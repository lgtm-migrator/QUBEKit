#!/usr/bin/env python

# TODO use proper terminal printing from helpers.
# TODO Force balance testing

from QUBEKit.decorators import timer_logger, for_all_methods

from simtk.openmm import app
import simtk.openmm as mm
from simtk import unit
from numpy import array, zeros, sqrt, sum, exp, round, append
from scipy.optimize import minimize

from subprocess import call as sub_call
from collections import OrderedDict
from copy import deepcopy
from os import chdir, mkdir, system
from shutil import rmtree

import matplotlib.pyplot as plt


# import seaborn as sns


@for_all_methods(timer_logger)
class TorsionScan:
    """This class will take a QUBEKit molecule object and perform a torsiondrive QM (and MM if True) energy scan
    for each selected dihedral.
    """

    def __init__(self, molecule, qm_engine, mm_engine='openmm', native_opt=False, verbose=False):

        self.qm_engine = qm_engine
        self.mm_engine = mm_engine
        self.constraints = None
        self.grid_space = qm_engine.fitting['increment']
        self.native_opt = native_opt
        self.verbose = verbose
        self.scan_mol = molecule
        self.cmd = {}
        self.find_scan_order()
        self.torsion_cmd()

    def find_scan_order(self):
        """Function takes the molecule and displays the rotatable central bonds,
        the user then enters the number of the torsions to be scanned in the order to be scanned.
        The molecule can also be supplied with a scan order already.
        """

        if self.scan_mol.scan_order:
            return self.scan_mol

        elif len(self.scan_mol.rotatable) == 1:
            print('One rotatable torsion found')
            self.scan_mol.scan_order = self.scan_mol.rotatable
            return self.scan_mol

        elif len(self.scan_mol.rotatable) == 0:
            print('No rotatable torsions found in the molecule')
            self.scan_mol.scan_order = []
            return self.scan_mol

        else:
            # Get the rotatable dihedrals from the molecule
            rotatable = list(self.scan_mol.rotatable)
            print('Please select the central bonds round which you wish to scan in the order to be scanned')
            print('Torsion number   Central-Bond   Representative Dihedral')
            # TODO Padding
            for i, bond in enumerate(rotatable):
                print(f'  {i + 1}                    {bond[0]}-{bond[1]}             '
                      f'{self.scan_mol.atom_names[self.scan_mol.dihedrals[bond][0][0] - 1]}-'
                      f'{self.scan_mol.atom_names[self.scan_mol.dihedrals[bond][0][1] - 1]}-'
                      f'{self.scan_mol.atom_names[self.scan_mol.dihedrals[bond][0][2] - 1]}-'
                      f'{self.scan_mol.atom_names[self.scan_mol.dihedrals[bond][0][3] - 1]}')

            scans = list(input('>'))  # Enter as a space separated list
            scans[:] = [scan for scan in scans if scan != ' ']  # remove all spaces from the scan list
            print(scans)

            scan_order = []
            # Add the rotatable dihedral keys to an array
            for scan in scans:
                scan_order.append(rotatable[int(scan) - 1])
            self.scan_mol.scan_order = scan_order

            return self.scan_mol

    def qm_scan_input(self, scan):
        """Function takes the rotatable dihedrals requested and writes a scan input file for torsiondrive."""

        with open('dihedrals.txt', 'w+') as out:

            out.write('# dihedral definition by atom indices starting from 0\n# i     j     k     l\n')
            scan_di = self.scan_mol.dihedrals[scan][0]
            out.write(f'  {scan_di[0]}     {scan_di[1]}     {scan_di[2]}     {scan_di[3]}\n')

        # TODO need to add PSI4 redundant mode selector

        if self.native_opt:
            self.qm_engine.generate_input(optimise=True, threads=True)

        else:
            self.qm_engine.geo_gradient(run=False, threads=True)

    def torsion_cmd(self):
        """Function generates a command strings to run torsiondrive based on the input commands for QM and MM."""

        # add the first basic command elements for QM
        cmd_qm = f'torsiondrive-launch {self.scan_mol.name}.{self.qm_engine.__class__.__name__.lower()}in dihedrals.txt '
        if self.grid_space:
            cmd_qm += f'-g {self.grid_space} '
        if self.qm_engine:
            cmd_qm += f'-e {self.qm_engine.__class__.__name__.lower()} '

        if self.native_opt:
            cmd_qm += '--native_opt '
        if self.verbose:
            cmd_qm += '-v '

        self.cmd = cmd_qm
        return self.cmd

    def get_energy(self, scan):
        """Function will extract an array of energies from the scan results
        and store it back into the molecule in a dictionary using the scan order as keys.
        """

        with open('scan.xyz', 'r') as scan_file:
            scan_energy = []
            for line in scan_file:
                if 'Energy ' in line:
                    scan_energy.append(float(line.split()[3]))

            self.scan_mol.QM_scan_energy[scan] = array(scan_energy)

            return self.scan_mol

    def start_scan(self):
        """Function makes a folder and writes a new a dihedral input file for each scan."""

        for scan in self.scan_mol.scan_order:
            mkdir(f'SCAN_{scan[0]}_{scan[1]}')
            chdir(f'SCAN_{scan[0]}_{scan[1]}')
            mkdir('QM')
            chdir('QM')

            # now make the scan input files
            self.qm_scan_input(scan)
            sub_call(self.cmd, shell=True)
            self.get_energy(scan)
            chdir('../')


# @for_all_methods(timer_logger)
class TorsionOptimiser:
    """Torsion optimiser class used to optimise dihedral parameters with a range of methods wieght_mm: wieght the low
    energy parts of the surface opls: use the opls combination rule use_force: match the forces as well as the
    energies step_size: the scipy displacement step size minimum error_tol: the scipy error tol x_tol: ? opt_method:
    the main scipy optmimzation method refinement_method: the extra refinment methods {SP: single point mathcing,
    Steep: steepest decent optimizer, None: no extra refinement.} """

    def __init__(self, molecule, qm_engine, config_dict, weight_mm=True, opls=True, use_force=False, step_size=0.02,
                 error_tol=1e-5, x_tol=1e-5, opt_method='BFGS', refinement_method='Steep', vn_bounds=20):
        self.qm, self.fitting, self.descriptions = config_dict[1:]
        self.l_pen = self.fitting['l_pen']
        self.t_weight = self.fitting['t_weight']
        self.molecule = molecule
        self.qm_engine = qm_engine
        self.opls = opls
        self.weight_mm = weight_mm
        self.step_size = step_size
        self.methods = {'NM': 'Nelder-Mead', 'BFGS': 'BFGS',
                        None: None}  # Scipy minimisation method; BFGS with custom step size
        self.method = self.methods[opt_method]
        self.error_tol = error_tol
        self.x_tol = x_tol
        self.energy_dict = molecule.QM_scan_energy
        self.use_Force = use_force
        self.mm_energy = []
        self.initial_energy = []
        self.scan_order = molecule.scan_order
        self.scan_coords = []
        self.starting_params = []
        self.energy_store_qm = []
        self.coords_store = []
        self.initial_coords = []
        self.atm_no = len(molecule.atom_names)
        self.system = None
        self.simulation = None
        self.target_energy = None
        self.qm_energy = None
        self.scan = None
        self.param_vector = None
        self.torsion_store = None
        self.abs_bounds = vn_bounds
        self.refinement = refinement_method
        self.index_dict = {}
        self.k_b = 0.001987
        self.tor_types = OrderedDict()
        self.phases = [0, 3.141594, 0, 3.141594]
        self.rest_torsions()
        self.openmm_system()

    def mm_energies(self):
        """Evaluate the MM energies of the QM structures."""

        self.mm_energy = []
        for position in self.scan_coords:
            # update the positions of the system
            self.simulation.context.setPositions(position)

            # Then get the energy from the new state
            state = self.simulation.context.getState(getEnergy=True, getForces=self.use_Force)
            # print(f'{float(str(state.getPotentialEnergy())[:-6])/4.184} kcal/mol')
            self.mm_energy.append(float(str(state.getPotentialEnergy())[:-6]) / 4.184)  # convert from kJ to kcal

        return array(self.mm_energy)
        # get forces from the system
        # open_grad = state.getForces()

    @staticmethod
    def get_coords(engine):
        """Read the torsion drive output file to get all of the coords in a format that can be passed to openmm
        so we can update positions in context without reloading the molecule."""

        scan_coords = []
        if engine == 'torsiondrive':
            # open the torsion drive data file read all the scan coordinates
            with open('qdata.txt', 'r') as data:
                for line in data.readlines():
                    if 'COORDS' in line:
                        # get the coords into a single array
                        coords = [float(x) / 10 for x in line.split()[1:]]
                        # convert to a list of tuples for OpenMM format
                        tups = []
                        for i in range(0, len(coords), 3):
                            tups.append((coords[i], coords[i + 1], coords[i + 2]))
                        scan_coords.append(tups)

        # get the coords from a geometric output
        elif engine == 'geometric':
            with open('scan-final.xyz', 'r') as data:
                lines = data.readlines()
                # get the amount of atoms
                atoms = int(lines[0])
                print(f'{atoms} atoms found!')
                for i, line in enumerate(lines):
                    if 'Iteration' in line:
                        # this is the start of the cordinates
                        tups = []
                        for coords in lines[i + 1:i + atoms + 1]:
                            coord = tuple(float(x) / 10 for x in coords.split()[1:])
                            # convert to a list of tuples for OpenMM format
                            # store tuples
                            tups.append(coord)
                        # now store that structure back to the coords list
                        scan_coords.append(tups)
        return scan_coords

    def openmm_system(self):
        """Initialise the OpenMM system we will use to evaluate the energies."""

        # Load the initial coords into the system and initialise
        pdb = app.PDBFile(self.molecule.filename)
        forcefield = app.ForceField(f'{self.molecule.name}.xml')
        modeller = app.Modeller(pdb.topology, pdb.positions)  # set the initial positions from the pdb
        self.system = forcefield.createSystem(modeller.topology, nonbondedMethod=app.NoCutoff, constraints=None)

        if self.opls:
            print('using opls rules')
            self.opls_lj()

        temperature = 298.15 * unit.kelvin
        integrator = mm.LangevinIntegrator(temperature, 5 / unit.picoseconds, 0.001 * unit.picoseconds)

        self.simulation = app.Simulation(modeller.topology, self.system, integrator)
        self.simulation.context.setPositions(modeller.positions)

    def initial_energies(self):
        """Calculate the initial energies using the input xml."""

        # first we need to work out the index order the torsions are in while inside the OpenMM system
        # this order is different from the xml order
        forces = {self.simulation.system.getForce(index).__class__.__name__: self.simulation.system.getForce(index) for
                  index in range(self.simulation.system.getNumForces())}
        torsion_force = forces['PeriodicTorsionForce']
        for i in range(torsion_force.getNumTorsions()):
            p1, p2, p3, p4, periodicity, phase, k = torsion_force.getTorsionParameters(i)
            torsion = (p1, p2, p3, p4)
            if torsion not in self.index_dict:
                self.index_dict[torsion] = i

        print(self.index_dict)

        # Now, reset all periodic torsion terms back to their initial values
        for pos, key in enumerate(self.torsion_store):
            try:
                self.tor_types[pos] = [[key], [float(self.torsion_store[key][i][1]) for i in range(4)],
                                       [self.index_dict[key]]]
            except KeyError:
                try:
                    self.tor_types[pos] = [[tuple(reversed(key))], [float(self.torsion_store[key][i][1]) for i in range(4)],
                                           [self.index_dict[tuple(reversed(key))]]]
                except KeyError:
                    # after trying to match the forward and backwards strings must be improper
                    self.tor_types[pos] = [[(key[1], key[2], key[0], key[3])], [float(self.torsion_store[key][i][1]) for i in range(4)],
                                           [self.index_dict[(key[1], key[2], key[0], key[3])]]]

        self.update_torsions()
        self.initial_energy = deepcopy(self.mm_energies())

        # Reset the dihedral values
        self.tor_types = OrderedDict()

    def update_tor_vec(self, x):
        """Update the tor_types dict with the parameter vector."""

        x = round(x, decimals=4)

        # Update the param vector for the right torsions by slicing the vector every 4 places
        for key, val in self.tor_types.items():
            val[1] = x[key * 4:key * 4 + 4]

    def objective(self, x):
        """Return the output of the objective function."""

        # Update the parameter vector into tor_types
        self.update_tor_vec(x)

        # Update the torsions in the Openmm system
        self.update_torsions()

        # Get the mm corresponding energy
        self.mm_energy = deepcopy(self.mm_energies())

        # Make sure the energies match
        assert len(self.qm_energy) == len(self.mm_energy)

        # calculate the objective

        # Adjust the mm energy to make it relative to the lowest in the scan
        mm_energy = self.mm_energy - min(self.mm_energy)
        error = (mm_energy - self.qm_energy) ** 2

        # if using a weighting, add that here
        if self.t_weight != 'infinity':
            error *= exp(-self.qm_energy / (self.k_b * self.t_weight))

        # Find the total error
        total_error = sqrt(sum(error) / len(self.scan_coords))

        # Calculate the penalties
        # 1 the movement away from the starting values
        move_pen = self.l_pen * sum((x - self.starting_params) ** 2)

        # 2 the penalty incurred by going past the bounds
        bounds_pen = 0
        for vn in x:
            if abs(vn) >= self.abs_bounds:
                bounds_pen += 1

        total_error += move_pen + bounds_pen
        print(f'total error: {total_error}\n move pen:{move_pen}\n bounds_pen:{bounds_pen}')
        return total_error

    def steep_objective(self, x):
        """Return the output of the objective function when using the steep refinment method."""

        # Update the parameter vector into tor_types
        self.update_tor_vec(x)
        print(x)

        # Update the torsions
        self.update_torsions()

        # first drive the torsion using geometric
        self.scan_coords = self.drive_mm(engine='geometric')

        # Get the mm corresponding energy
        self.mm_energy = self.mm_energies()

        # Make sure the energies match
        assert len(self.qm_energy) == len(self.mm_energy)

        # calculate the objective

        # Adjust the mm energy to make it relative to the lowest in the scan
        self.mm_energy -= min(self.mm_energy)
        error = (self.mm_energy - self.qm_energy) ** 2

        # if using a weighting, add that here
        if self.t_weight != 'infinity':
            error *= exp(-self.qm_energy / (self.k_b * self.t_weight))

        # Find the total error
        total_error = sqrt(sum(error) / len(self.scan_coords))

        # Calculate the penalty
        pen = self.l_pen * sum((x - self.starting_params) ** 2)
        total_error += pen

        print(total_error)

        return total_error

    def single_point_matching(self, fitting_error, opt_parameters):
        """A function the call the single point matching method of parameter refinement.

        method
        -------------------
        1) take parameters from the initial scipy fitting.
        2) Do a MM torsion scan with the parameters and get the rmsd error
        3) Calculate the QM single point energies from the structures and get the energy error
        4) Calculate the total error if not converged fit using scipy and move to step 2)
        """

        converged = False

        # put in the objective dict
        objective = {'fitting error': [],
                     'energy error': [],
                     'rmsd': [],
                     'total': [],
                     'parameters': []}

        # # do MM surface scan
        # print('getting inital rmsd')
        # # with wavefront propagation, returns the new set of coords these become the new scan coords
        # self.scan_coords = self.drive_mm(engine='torsiondrive')
        #
        # # calculate the energy error
        # energy_error = self.objective(opt_parameters)
        #
        # # calculate the rmsd error of the structures compared to the QM
        # rmsd = self.rmsd('scan.xyz', 'torsiondrive_scan/scan.xyz')

        iteration = 1
        # start the main optimizer loop by calculating new single point energies
        while not converged:

            # step 2 MM torsion scan
            # with wavefront propagation, returns the new set of coords these become the new scan coords
            self.scan_coords = self.drive_mm(engine='torsiondrive')

            # step 3 calculate the rmsd for these structures compared to QM
            rmsd = self.rmsd('scan.xyz', 'torsiondrive_scan/scan.xyz')

            # step 4 calculate the single point energies
            # Calculate the single point energies of each of the positions returned
            # Using the qm_engine, store back into the qm_energy as the new reference
            self.qm_energy = self.single_point()

            # Normalise the qm energy again
            self.qm_energy -= min(self.qm_energy)  # make relative to lowest energy
            self.qm_energy *= 627.509  # convert to kcal/mol

            # calculate the energy error in step 4 (just for this scan) and get a measure of the new reference energies
            energy_error = self.objective(opt_parameters)

            # add the results to the dictionary
            objective['fitting error'].append(fitting_error)
            objective['energy error'].append(energy_error)
            objective['rmsd'].append(rmsd)
            objective['total'].append(energy_error + rmsd)
            objective['parameters'].append(opt_parameters)

            # now check to see if the error has converged?
            if iteration < 3:
                # if (energy_error + rmsd - objective['total'][-1]) < 0 and\
                #         abs(energy_error + rmsd - objective['total'][-1]) > 0.01:

                # get the energy surface made by the current parameters this acts as the new initial
                # surface
                self.initial_energy = deepcopy(self.mm_energies())

                # we need to now reoptimize the parameters and start the loop again
                # optimise using the scipy method
                fitting_error, opt_parameters = self.scipy_optimiser()
                print(f'The current error: {fitting_error}\n opt parameters: {opt_parameters}')

                # update the parameters in the fitting vector and the molecule for the MM scans
                self.update_tor_vec(opt_parameters)
                self.update_mol()

                # do a plot at each iteration
                self.plot_results(name=f'SP_fit_iter{iteration}')

                # add 1 to the iteration
                iteration += 1
            else:
                break

        print(objective)
        print(f'The error converged after {iteration} iterations.')
        # find the minimum total error index in list
        min_error = min(objective['total'])
        min_index = objective['total'].index(min_error)

        # gather the parameters with the lowest error, not always the last parameter set
        final_parameters = deepcopy(objective['parameters'][min_index])
        final_error = objective['total'][min_index]

        # get the energy surface for these parameters and update the parameters in the molecule
        energy_error = self.objective(final_parameters)

        # add a convergence plot as well to monitor the progress of the convergence of the fitting
        self.convergence_plot(name='Single_point_convergence', objective_dict=objective)

        # plot the results
        self.plot_results(name='Stage2_Single_point_fit')

        return final_error, final_parameters

    def single_point_matching_2(self, fitting_error, opt_parameters):
        """A function the call the single point matching method of parameter refinement.

        method
        -------------------
        1) take parameters from the initial scipy fitting.
        2) Do a MM torsion scan with the parameters and get the rmsd error
        3) Calculate the QM single point energies from the structures and get the energy error
        4) Calculate the total error if not converged fit using scipy to all structures and move to step 2)
        """

        converged = False

        # put in the objective dict
        objective = {'fitting error': [],
                     'energy error': [],
                     'rmsd': [],
                     'total': [],
                     'parameters': []}

        # # do MM surface scan
        # print('getting inital rmsd')
        # # with wavefront propagation, returns the new set of coords these become the new scan coords
        # self.scan_coords = self.drive_mm(engine='torsiondrive')
        #
        # # calculate the energy error
        # energy_error = self.objective(opt_parameters)
        #
        # # calculate the rmsd error of the structures compared to the QM
        # rmsd = self.rmsd('scan.xyz', 'torsiondrive_scan/scan.xyz')

        iteration = 1
        # start the main optimizer loop by calculating new single point energies
        while not converged:

            # step 2 MM torsion scan
            # with wavefront propagation, returns the new set of coords these become the new scan coords
            self.scan_coords = self.drive_mm(engine='torsiondrive')

            # also save these coords to the coords store
            self.coords_store = deepcopy(self.coords_store + self.scan_coords)

            # step 3 calculate the rmsd for these structures compared to QM
            rmsd = self.rmsd('scan.xyz', 'torsiondrive_scan/scan.xyz')

            # step 4 calculate the single point energies
            # Calculate the single point energies of each of the positions returned
            # Using the qm_engine, store back into the qm_energy as the new reference
            self.qm_energy = self.single_point()

            # Keep a copy of the energy before adjusting in case another loop is needed
            self.energy_store_qm = deepcopy(append(self.energy_store_qm, self.qm_energy))
            print(self.energy_store_qm)

            # Normalise the qm energy again
            self.qm_energy -= min(self.qm_energy)  # make relative to lowest energy
            self.qm_energy *= 627.509  # convert to kcal/mol
            # print the normalized qm energy

            # calculate the energy error in step 4 (just for this scan) and get a measure of the new reference energies
            energy_error = self.objective(opt_parameters)
            # this now acts as the intial energy for the next fit
            self.initial_energy = deepcopy(self.mm_energy)

            # add the results to the dictionary
            objective['fitting error'].append(fitting_error)
            objective['energy error'].append(energy_error)
            objective['rmsd'].append(rmsd)
            objective['total'].append(energy_error + rmsd)
            objective['parameters'].append(opt_parameters)

            # now check to see if the error has converged?
            if iteration < 3:
                # if (energy_error + rmsd - objective['total'][-1]) < 0 and\
                #         abs(energy_error + rmsd - objective['total'][-1]) > 0.01:

                # now we don't want to move to far away from the last set of optimized parameters
                self.starting_params = opt_parameters
                # turn on the penalty
                self.l_pen = 0.05

                # optimise using the scipy method for the new structures with a penatly to remain close to the old
                fitting_error, opt_parameters = self.scipy_optimiser()
                print(f'The current error: {fitting_error}\n opt parameters: {opt_parameters}')

                # update the parameters in the fitting vector and the molecule for the MM scans
                self.update_tor_vec(opt_parameters)
                self.update_mol()

                # use the parameters to get the current energies
                self.mm_energy = deepcopy(self.mm_energies())

                # plot the fitting graph this iteration
                self.plot_results(name=f'SP_iter_{iteration}')

                # now get the correlation data
                self.scan_coords = deepcopy(self.coords_store)
                print(f'scan coords length {len(self.scan_coords)}')
                self.qm_energy = deepcopy(self.energy_store_qm)

                # now reset the energy's
                # Normalise the qm energy again
                self.qm_energy -= min(self.qm_energy)  # make relative to lowest energy
                self.qm_energy *= 627.509  # convert to kcal/mol
                print(self.qm_energy)

                # calculate the energy's of all of the structures
                total_single_error = self.objective(opt_parameters)
                print(self.mm_energy)
                print(total_single_error)

                # plot the single point energy corelation graph
                self.plot_corelation(name=f'SP_corel_{iteration}')
                exit()
                # add 1 to the iteration
                iteration += 1
            else:
                break

        print(objective)
        print(f'The error converged after {iteration} iterations.')
        # find the minimum total error index in list
        min_error = min(objective['total'])
        min_index = objective['total'].index(min_error)

        # gather the parameters with the lowest error, not always the last parameter set
        final_parameters = deepcopy(objective['parameters'][min_index])
        final_error = objective['total'][min_index]

        # now we want to see how well we have captured the initial QM energy surface
        # reset the scan coords to the initial values
        self.scan_coords = self.initial_coords

        # get the energy surface for these final parameters
        # this will also update the parameters in the molecule class so we can write a new xml
        energy_error = self.objective(final_parameters)

        # plot the results this is a graph of the starting QM surface and how well we can remake it
        self.plot_results(name='Stage2_Single_point_fit')

        return final_error, final_parameters

    def plot_corelation(self, name):
        """Plot the single point energy correlation."""

        # Make sure we have the same number of energy terms in the QM and MM lists
        assert len(self.qm_energy) == len(self.mm_energy)

        # make it relative to the lowest qm energy
        qm_min = min(self.qm_energy)
        qm_index = self.qm_energy.index(qm_min)
        # adjust the mm_energy but do not alter
        mm_energy = self.mm_energy - self.mm_energy[qm_index]

        # now we are just ploting them against each other they are already in the right order
        plt.scatter(mm_energy, self.qm_energy)

        plt.xlabel('Relative energy (kcal/mol) MM energy')
        plt.ylabel('Relative energy (kcal/mol) QM energy')
        plt.savefig(f'{name}.pdf')
        plt.clf()

    def run(self):
        """Optimize the parameters for the chosen torsions in the molecule scan_order,
        also set up a work queue to do the single point calculations if they are needed."""

        # Set up the first fitting
        for self.scan in self.scan_order:
            # Set the target energies first
            self.target_energy = self.energy_dict[self.scan]

            # Adjust the QM energies
            # and store all QM raw energies
            self.energy_store_qm = deepcopy(self.target_energy)
            self.qm_energy = deepcopy(self.target_energy)
            self.qm_energy -= min(self.qm_energy)  # make relative to lowest energy
            self.qm_energy *= 627.509  # convert to kcal/mol

            # Get the MM coords from the QM torsion drive
            self.scan_coords = self.get_coords(engine='torsiondrive')

            # Keep the initial coords
            self.coords_store = deepcopy(self.scan_coords)
            self.initial_coords = deepcopy(self.scan_coords)

            # Get the initial energies
            self.initial_energies()

            # Get the torsions that will be fit and make the param vector
            self.get_torsion_params()

            # Start the main optimiser loop and get the final error and parameters back
            error, opt_parameters = self.scipy_optimiser()
            print(f'The current error: {error}\n opt parameters: {opt_parameters}')
            self.param_vector = opt_parameters

            # Push the new parameters back to the molecule parameter dictionary
            self.update_mol()

            # Plot the results of the first fit
            self.plot_results(name='Stage1_scipy')

            if self.refinement == 'SP':
                error, opt_parameters = self.single_point_matching_2(error, opt_parameters)
                self.param_vector = opt_parameters

            elif self.refinement == 'Steep':
                error, opt_parameters = self.steepest_decent_refinement(self.param_vector)

            # now push the parameters back to the molecule
            self.update_tor_vec(opt_parameters)
            self.update_mol()

        # TODO 2D scans?

    def steepest_decent_refinement(self, x):
        """A steepest decent optimiser as implemented in QUBEKit-V1, which will optimise the torsion terms
         using full relaxed surface scans. SLOW!"""

        print('Starting optimization....')

        # search steep sizes
        step_size = [0.1, 0.01, 0.001]
        step_index = 0

        # set convergence
        converged = False

        # start main optimizer loop
        while not converged:

            # when to change the step size
            un_changed = 0

            # for each Vn parameter in the parameter vector
            for i in range(len(x)):

                # error dict
                error = {}

                # First we need to get the initial error with a full relaxed scan
                self.scan_coords = self.drive_mm(engine='geometric')

                # get the starting energies and errors from the current parameter set
                normal = self.objective(x)

                error[normal] = x
                # make a copy of the parameter vector
                y_plus = deepcopy(x)

                # now make a variation on the parameter set
                y_plus[i] += step_size[step_index]
                print(f'y plus {y_plus}')
                # now find the new error
                self.scan_coords = self.drive_mm(engine='geometric')

                error_plus = self.objective(y_plus)
                error[error_plus] = y_plus

                # now make a differnt variation
                y_minus = deepcopy(x)
                y_minus[i] -= step_size[step_index]
                print(f'y minus {y_minus}')

                # now find the other error
                self.scan_coords = self.drive_mm(engine='geometric')
                error_minus = self.objective(y_minus)
                error[error_minus] = y_minus

                # now work out which has the lowest error
                min_error = min(normal, error_plus, error_minus)
                print(f'minimum error {min_error}')

                # now the parameter vector becomes who had the lowest error
                x = deepcopy(error[min_error])
                print(f'The new parameter vector {x}')

                # if the error is not changed count how many times this happens
                if min_error == normal:
                    # add one to unchanged
                    un_changed += 1

                # if all Vn have no effect then change the step size
                if un_changed == len(x) - 1:
                    step_index += 1

                # now check to see if we have ran out steps
                if step_index >= len(step_size):
                    opt_parameters = deepcopy(x)
                    error = deepcopy(min_error)
                    break

        return error, opt_parameters

    def rest_torsions(self):
        """Set all the torsion k values to one for every torsion in the system.

        Once an OpenMM system is created we cannot add new torsions without making a new PeriodicTorsion
        force every time.

        To get round this we have to load every k parameter into the system first; so we set every k term in the fitting
        dihedrals to 1 then reset all values to the gaff terms and update in context.
        """

        # save the molecule torsions to a dict
        self.torsion_store = deepcopy(self.molecule.PeriodicTorsionForce)

        # Set all the torsion to 1 to get them into the system
        # TODO .keys() / .items() ?
        for key in self.molecule.PeriodicTorsionForce:
            if self.molecule.PeriodicTorsionForce[key][-1] == 'Improper':
                self.molecule.PeriodicTorsionForce[key] = [['1', '1', '0'], ['2', '1', '3.141594'],
                                                           ['3', '1', '0'], ['4', '1', '3.141594'], 'Improper']
            else:
                self.molecule.PeriodicTorsionForce[key] = [['1', '1', '0'], ['2', '1', '3.141594'],
                                                           ['3', '1', '0'], ['4', '1', '3.141594']]

        print(self.molecule.PeriodicTorsionForce)
        # Write out the new xml file which is read into the OpenMM system
        self.molecule.write_parameters()

        # Put the torsions back into the molecule
        self.molecule.PeriodicTorsionForce = deepcopy(self.torsion_store)

    def get_torsion_params(self):
        """Get the torsions and their parameters that will scanned, work out how many different torsion types needed,
        make a vector corresponding to this size."""

        # TODO check the atom types are the same as well as the parameters
        # Get a list of which dihedrals parameters are to be varied
        # Convert to be indexed from 0
        to_fit = [(tor[0] - 1, tor[1] - 1, tor[2] - 1, tor[3] - 1) for tor in list(self.molecule.dihedrals[self.scan])]
        print(to_fit)

        # Check which ones have the same parameters and how many torsion vectors we need
        self.tor_types = OrderedDict()

        # List of torsion keys to index
        tor_key = list(self.torsion_store.keys())

        i = 0
        while to_fit:
            # Get the current torsion
            torsion = to_fit.pop(0)

            # Get the torsions param vector used to compare to others
            # The master vector could be backwards so try one way and if keyerror try the other
            try:
                master_vector = [float(self.torsion_store[torsion][i][1]) for i in range(4)]
            except KeyError:
                torsion = torsion[::-1]
                master_vector = [float(self.torsion_store[torsion][i][1]) for i in range(4)]

            # Add this type to the torsion type dictionary with the right key index
            try:
                self.tor_types[i] = [[torsion], master_vector, [self.index_dict[torsion]]]
            except KeyError:
                self.tor_types[i] = [[torsion], master_vector, [self.index_dict[tuple(reversed(torsion))]]]

            to_remove = []
            # Iterate over what is left of the list to see what other torsions are the same as the master
            for dihedral in to_fit:
                # Again, try both directions
                try:
                    vector = [float(self.torsion_store[dihedral][i][1]) for i in range(4)]
                except KeyError:
                    dihedral = dihedral[::-1]
                    vector = [float(self.torsion_store[dihedral][i][1]) for i in range(4)]

                # See if that vector is the same as the master vector
                if vector == master_vector:
                    try:
                        self.tor_types[i][2].append(self.index_dict[dihedral])
                        self.tor_types[i][0].append(dihedral)
                    except KeyError:
                        self.tor_types[i][2].append(self.index_dict[tuple(reversed(dihedral))])
                        self.tor_types[i][0].append(tuple(reversed(dihedral)))
                    to_remove.append(dihedral)

            # Remove all of the dihedrals that have been matched
            for dihedral in to_remove:
                try:
                    to_fit.remove(dihedral)
                except ValueError:
                    to_fit.remove(dihedral[::-1])
            i += 1

        # now that we have grouped by param vectors we need to compare the elements that make up the torsions
        # then if they are different we need to further split the torsions
        # first construct the dictionary of type strings
        torsion_string_dict = {}
        for index, tor_info in self.tor_types.items():
            for j, torsion in enumerate(tor_info[0]):
                # get the tuple of the torsion string
                tor_tup = tuple(self.molecule.AtomTypes[torsion[i]][3] for i in range(4))
                # check if its in the torsion string dict
                try:
                    torsion_string_dict[tor_tup][0].append(torsion)
                    torsion_string_dict[tor_tup][2].append(tor_info[2][j])
                except KeyError:
                    try:
                        torsion_string_dict[tuple(reversed(tor_tup))][0].append(torsion)
                        torsion_string_dict[tuple(reversed(tor_tup))][2].append(tor_info[2][j])
                    except KeyError:
                        torsion_string_dict[tor_tup] = [[torsion], tor_info[1], [tor_info[2][j]]]

        self.tor_types = OrderedDict((index, k) for index, k in enumerate(torsion_string_dict.values()))

        # Make the param_vector of the correct size
        self.param_vector = zeros((1, len(list(self.tor_types.keys())) * 4))

        # now take the master vectors and make the starting parameter list
        # Store the original parameter vectors to use regularisation
        self.starting_params = [list(k)[1][i] for k in self.tor_types.values() for i in range(4)]

    def rmsd(self, qm_coords, mm_coords):
        """Calculate the rmsd between the MM and QM predicted structures from the relaxed scans using pymol;
        this can be added into the penalty function."""

        print('starting rmsd')

        import __main__
        __main__.pymol_argv = ['pymol', '-qc']  # Quiet and no GUI

        from pymol import cmd as py_cmd
        from pymol import finish_launching

        print('turing of gui')

        print('imports done!')
        finish_launching()
        py_cmd.load(mm_coords, object='MM_scan')
        py_cmd.load(qm_coords, object='QM_scan')
        rmsd = py_cmd.align('MM_scan', 'QM_scan')[0]
        print(f'rmsd: {rmsd}')
        # now remove the objects from the pymol instance
        py_cmd.delete('MM_scan')
        py_cmd.delete('QM_scan')
        print('rmsd function done!')

        return rmsd

    def finite_difference(self, x):
        """Compute the gradient of changing the parameter vector using central difference scheme."""

        gradient = []
        for i in range(len(x)):
            x[i] += self.step_size / 2
            plus = self.objective(x)
            x[i] -= self.step_size
            minus = self.objective(x)
            diff = (plus - minus) / self.step_size
            gradient.append(diff)
        return array(gradient)

    def scipy_optimiser(self):
        """The main torsion parameter optimiser that controls the optimisation method used."""

        print(f'Running scipy {self.method} optimiser ... ')

        # Does not work in dictionary for some reason
        # TODO Try .get() ?
        if self.method == 'Nelder-Mead':
            res = minimize(self.objective, self.param_vector, method='Nelder-Mead',
                           options={'xtol': self.x_tol, 'ftol': self.error_tol, 'disp': True})

        elif self.method == 'BFGS':
            res = minimize(self.objective, self.param_vector, method='BFGS', jac=self.finite_difference,
                           options={'disp': True})

        else:
            raise NotImplementedError('The optimisation method is not implemented')

        print('Scipy optimisation complete')

        # Update the tor types dict using the optimised vector
        self.update_tor_vec(res.x)

        # return the final fitting error and final param vector after the optimisation
        return res.fun, res.x

    def use_forcebalance(self):
        """Call force balance to do the single point energy matching."""

        pass

    def update_torsions(self):
        """Update the torsions being fitted."""

        forces = {self.simulation.system.getForce(index).__class__.__name__: self.simulation.system.getForce(index) for
                  index in range(self.simulation.system.getNumForces())}
        torsion_force = forces['PeriodicTorsionForce']
        i = 0
        for key, val in self.tor_types.items():
            for j, dihedral in enumerate(val[0]):
                for v_n in range(4):
                    # print the torsion we are replacing and the new torsion
                    # print(torsion_force.getTorsionParameters(v_n + val[2][j]))
                    # print(f'{tuple(dihedral[i] for i in range(4))}')
                    torsion_force.setTorsionParameters(index=v_n + val[2][j],
                                                       particle1=dihedral[0], particle2=dihedral[1],
                                                       particle3=dihedral[2], particle4=dihedral[3],
                                                       periodicity=v_n + 1, phase=self.phases[v_n],
                                                       k=val[1][v_n])
                    i += 1
        torsion_force.updateParametersInContext(self.simulation.context)

        return self.system

    @staticmethod
    def convergence_plot(name, objective_dict):
        """Plot the convergence of the errors of the fitting."""

        # sns.set()

        # this will be a plot with multipul lines showing the convergence of the errors with each iteration
        iterations = [x for x in range(len(objective_dict['total']))]
        rmsd = objective_dict['rmsd']
        fitting_error = objective_dict['fitting error']
        energy_error = objective_dict['energy error']
        total_error = objective_dict['total']

        plt.plot(iterations, energy_error, label='SP energy error')
        plt.plot(iterations, rmsd, label='Rmsd error')
        plt.plot(iterations, fitting_error, label='Fitting error')
        plt.plot(iterations, total_error, label='Total error')

        plt.ylabel('Error (kcal/mol)')
        plt.xlabel('Iteration')
        plt.legend()
        plt.savefig(f'{name}.pdf')
        plt.clf()

    def plot_test(self, energies):
        """Plot the results of the fitting."""

        # sns.set()

        # Make sure we have the same number of energy terms in the QM and MM lists
        assert len(self.qm_energy) == len(self.mm_energy)

        # Now adjust the MM energies
        # self.mm_energy -= min(self.mm_energy)
        # self.mm_energy /= 4.184 # convert from kj to kcal

        # Make the angle array
        angles = [x for x in range(-165, 195, self.qm_engine.fitting['increment'])]
        plt.plot(angles, self.qm_energy, 'o', label='QM')
        for i, scan in enumerate(energies):
            self.mm_energy = array(scan)
            self.mm_energy -= min(self.mm_energy)
            plt.plot(angles, self.mm_energy, label=f'MM{i}')
        plt.ylabel('Relative energy (kcal/mol')
        plt.xlabel('Dihedral angle$^{\circ}$')
        plt.legend()
        plt.savefig('Plot.pdf')

    def plot_results(self, name='Plot', validate=False):
        """Plot the results of the scan."""

        # sns.set()

        # Make sure we have the same number of energy terms in the QM and MM lists
        assert len(self.qm_energy) == len(self.mm_energy)

        # Adjust the MM energies
        plot_mm_energy = self.mm_energy - min(self.mm_energy)

        # Adjust the initial MM energies
        initial_energy = self.initial_energy - min(self.initial_energy)

        # Construct the angle array
        angles = [x for x in range(-165, 195, self.qm_engine.fitting['increment'])]

        if len(self.qm_energy) > len(angles):
            points = [x for x in range(len(self.qm_energy))]
        else:
            points = None

        if points is not None:
            # Print a table of the results for multiple plots
            print(f'Geometry    QM(relative)        MM(relative)    MM_initial(relative)')
            for i in points:
                print(f'{i:4}  {self.qm_energy[i]:15.10f}     {plot_mm_energy[i]:15.10f}    {initial_energy[i]:15.10f}')

            # Plot the qm and mm data
            plt.plot(points, self.qm_energy, 'o', label='QM')
            plt.plot(points, initial_energy, label='MM initial')
            plt.plot(points, plot_mm_energy, label=f'MM final')

            plt.xlabel('Geometry')

        else:
            # Print a table of the results
            print(f'Angle    QM(relative)        MM(relative)    MM_initial(relative)')
            for pos, angle in enumerate(angles):
                print(
                    f'{angle:4}  {self.qm_energy[pos]:15.10f}     {plot_mm_energy[pos]:15.10f}    {initial_energy[pos]:15.10f}')

            plt.xlabel('Dihedral angle$^{\circ}$')

            # Plot the qm and mm data
            plt.plot(angles, self.qm_energy, 'o', label='QM')
            if not validate:
                plt.plot(angles, initial_energy, label='MM initial')
                plt.plot(angles, plot_mm_energy, label='MM final')

            else:
                plt.plot(angles, plot_mm_energy, label='MM validate')

        # Label the graph and save the pdf
        plt.ylabel('Relative energy (kcal/mol)')
        plt.legend(loc=1)
        plt.savefig(f'{name}.pdf')
        plt.clf()

    def make_constraints(self):
        """Write a constraint file used by geometric during optimizations."""

        with open('constraints.txt', 'w+')as constraint:
            constraint.write(
                f'$scan\ndihedral {self.molecule.dihedrals[self.scan][0][0]} {self.molecule.dihedrals[self.scan][0][1]}'
                f' {self.molecule.dihedrals[self.scan][0][2]} {self.molecule.dihedrals[self.scan][0][3]} -165.0 180 24\n')

    def write_dihedrals(self):
        """Write out the torsion drive dihedral file for the current self.scan."""

        with open('dihedrals.txt', 'w+') as out:
            out.write('# dihedral definition by atom indices starting from 0\n# i     j     k     l\n')
            mol_di = self.molecule.dihedrals[self.scan][0]
            out.write(f'  {mol_di[0]}     {mol_di[1]}     {mol_di[2]}     {mol_di[3]}\n')

    def drive_mm(self, engine):
        """Drive the torsion again using MM to get new structures."""

        # Create a temporary working directory to call torsion drive from
        # Write an xml file with the new parameters

        # Move into a temporary folder torsion drive gives an error if we use tempdirectory module
        temp = f'{engine}_scan'
        try:
            rmtree(temp)
        except FileNotFoundError:
            pass
        mkdir(temp)
        chdir(temp)

        # Write out a pdb file of the qm optimised geometry
        self.molecule.write_pdb(name='openmm')
        # Also need an xml file for the molecule to use in geometric
        self.molecule.write_parameters(name='input')
        # openmm.pdb and input.xml are the expected names for geometric
        with open('log.txt', 'w+')as log:
            if engine == 'torsiondrive':
                self.write_dihedrals()
                completed = system('torsiondrive-launch -e openmm openmm.pdb dihedrals.txt > log.txt')
                if completed == 0:
                    print('sucessful!!!')
                # sub_call('torsiondrive-launch -e openmm openmm.pdb dihedrals.txt', shell=True, stdout=log)
                positions = self.get_coords(engine='torsiondrive')
            elif engine == 'geometric':
                self.make_constraints()
                sub_call('geometric-optimize --reset --epsilon 0.0 --maxiter 500 --qccnv --openmm openmm.pdb constraints.txt', shell=True, stdout=log)
                positions = self.get_coords(engine='geometric')
            else:
                raise NotImplementedError

        # move back to the master folder
        chdir('../')

        # return the new positions
        return positions

    def single_point(self):
        """Take set of coordinates of a molecule and do a single point calculation; returns an array of the energies."""

        sp_energy = []
        # for each coordinate in the system we need to write a qm input file and get the single point energy
        # TODO add progress bar (tqdm?)
        try:
            rmtree(f'Single_points')
        except FileNotFoundError:
            pass
        mkdir('Single_points')
        chdir('Single_points')
        for i, x in enumerate(self.scan_coords):
            mkdir(f'SP_{i}')
            chdir(f'SP_{i}')
            print(f'Doing single point calculations on new structures ... {i + 1}/{len(self.scan_coords)}')
            # now we need to change the positions of the molecule in the molecule array
            for y, coord in enumerate(x):
                for z, pos in enumerate(coord):
                    self.qm_engine.molecule.molecule[y][
                        z + 1] = pos * 10  # convert from nanometers in openmm to A in QM

            # Write the new coordinate file and run the calculation
            self.qm_engine.generate_input(energy=True)

            # Extract the energy and save to the array
            sp_energy.append(self.qm_engine.get_energy())

            # Move back to the base directory
            chdir('../')

        # move out to the main folder
        chdir('../')
        # return the array of the new single point energies
        return array(sp_energy)

    def update_mol(self):
        """When the optimization is complete update the PeriodicTorsionForce parameters in the molecule."""

        for key, val in self.tor_types.items():
            for dihedral in val[0]:
                for vn in range(4):
                    try:
                        self.molecule.PeriodicTorsionForce[dihedral][vn][1] = str(val[1][vn])
                    except KeyError:
                        self.molecule.PeriodicTorsionForce[tuple(reversed(dihedral))][vn][1] = str(val[1][vn])

    def opls_lj(self, excep_pairs=None, normal_pairs=None):
        """This function changes the standard OpenMM combination rules to use OPLS, execp and normal pairs are only
        required if their are virtual sites in the molecule.
        """

        # Get the system information from the openmm system
        forces = {self.system.getForce(index).__class__.__name__: self.system.getForce(index) for index in
                  range(self.system.getNumForces())}
        # Use the nondonded_force to get the same rules
        nonbonded_force = forces['NonbondedForce']
        lorentz = mm.CustomNonbondedForce(
            'epsilon*((sigma/r)^12-(sigma/r)^6); sigma=sqrt(sigma1*sigma2); epsilon=sqrt(epsilon1*epsilon2)*4.0')
        lorentz.setNonbondedMethod(nonbonded_force.getNonbondedMethod())
        lorentz.addPerParticleParameter('sigma')
        lorentz.addPerParticleParameter('epsilon')
        lorentz.setCutoffDistance(nonbonded_force.getCutoffDistance())
        self.system.addForce(lorentz)

        l_j_set = {}
        # For each particle, calculate the combination list again
        for index in range(nonbonded_force.getNumParticles()):
            charge, sigma, epsilon = nonbonded_force.getParticleParameters(index)
            l_j_set[index] = (sigma, epsilon)
            lorentz.addParticle([sigma, epsilon])
            nonbonded_force.setParticleParameters(index, charge, sigma, epsilon * 0)

        for i in range(nonbonded_force.getNumExceptions()):
            (p1, p2, q, sig, eps) = nonbonded_force.getExceptionParameters(i)
            # ALL THE 12,13 and 14 interactions are EXCLUDED FROM CUSTOM NONBONDED FORCE
            lorentz.addExclusion(p1, p2)
            if eps._value != 0.0:
                sig14 = sqrt(l_j_set[p1][0] * l_j_set[p2][0])
                # TODO eps14 not used
                eps14 = sqrt(l_j_set[p1][1] * l_j_set[p2][1])
                nonbonded_force.setExceptionParameters(i, p1, p2, q, sig14, eps)
            # If there is a virtual site in the molecule we have to change the exceptions and pairs lists
            # Old method which needs updating
            if excep_pairs:
                for x in range(len(excep_pairs)):  # scale 14 interactions
                    if p1 == excep_pairs[x, 0] and p2 == excep_pairs[x, 1] or p2 == excep_pairs[x, 0] and p1 == \
                            excep_pairs[x, 1]:
                        charge1, sigma1, epsilon1 = nonbonded_force.getParticleParameters(p1)
                        charge2, sigma2, epsilon2 = nonbonded_force.getParticleParameters(p2)
                        q = charge1 * charge2 * 0.5
                        sig14 = sqrt(sigma1 * sigma2) * 0.5
                        eps = sqrt(epsilon1 * epsilon2) * 0.5
                        nonbonded_force.setExceptionParameters(i, p1, p2, q, sig14, eps)

            if normal_pairs:
                for x in range(len(normal_pairs)):
                    if p1 == normal_pairs[x, 0] and p2 == normal_pairs[x, 1] or p2 == normal_pairs[x, 0] and p1 == \
                            normal_pairs[x, 1]:
                        charge1, sigma1, epsilon1 = nonbonded_force.getParticleParameters(p1)
                        charge2, sigma2, epsilon2 = nonbonded_force.getParticleParameters(p2)
                        q = charge1 * charge2
                        sig14 = sqrt(sigma1 * sigma2)
                        eps = sqrt(epsilon1 * epsilon2)
                        nonbonded_force.setExceptionParameters(i, p1, p2, q, sig14, eps)

        return self.system
