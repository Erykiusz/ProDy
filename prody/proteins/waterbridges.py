# -*- coding: utf-8 -*-

"""This module detects, predicts and analyzes water bridges.
"""

__author__ = 'Karolina Mikulska-Ruminska'
__credits__ = ['Frane Doljanin', 'Karolina Mikulska-Ruminska']
__email__ = ['karolamik@fizyka.umk.pl', 'fdoljanin@pmfst.hr']

import numpy as np
from scipy.sparse import lil_matrix
from itertools import combinations
from collections import deque
from enum import Enum, auto

from prody import LOGGER
from prody.atomic import Atom, Atomic, AtomGroup
from prody.measure import calcAngle, calcDistance
from prody.measure.contacts import findNeighbors
from prody.proteins import writePDB, parsePDB
from prody.ensemble import Ensemble

# from timeit import default_timer as timer


__all__ = ['calcWaterBridges', 'calcWaterBridgesTrajectory',
           'calcWaterBridgesStatistics', 'calcWaterBridgingResidues', 'savePDBWaterBridges']


class ResType(Enum):
    WATER = auto()
    PROTEIN = auto()


class AtomNode:
    def __init__(self, atom, type):
        self.atom = atom
        self.hydrogens = []
        self.type = type
        self.bonds = []
        self.bondedAtoms = []
        self.isVisited = False


class RelationList:
    def __init__(self, numNodes):
        self.nodes = [None for _ in range(numNodes)]

    def resetVisits(self):
        for node in self.nodes:
            if node:
                node.isVisited = False

    def addNode(self, atom, type):
        self[atom.getIndex()] = AtomNode(atom, type)

        return self[atom]

    def addHBond(self, bond):
        donor, acceptor = bond.donor, bond.acceptor
        self[donor].bonds.append(bond)
        self[acceptor].bonds.append(bond)

        if acceptor.getIndex() not in map(lambda a: a.getIndex(), self[donor].bondedAtoms):
            self[donor].bondedAtoms.append(acceptor)
        if donor.getIndex() not in map(lambda a: a.getIndex(), self[acceptor].bondedAtoms):
            self[acceptor].bondedAtoms.append(donor)

    def removeUnnecessary(self):
        for i in range(len(self.nodes)):
            if self.nodes[i] and not self.nodes[i].bonds:
                self[i] = None

    def __getitem__(self, key):
        if isinstance(key, Atom):
            key = key.getIndex()

        return self.nodes[key]

    def __setitem__(self, key, value):
        self.nodes[key] = value


class HBondConstraints:
    def __init__(self, donors, acceptors, angleWW=None, anglePAWD=None, anglePDWA=None):
        self.donors = donors
        self.acceptors = acceptors
        self.angleWW = angleWW
        self.anglePAWD = anglePAWD
        self.anglePDWA = anglePDWA

    def addAngles(self, angleWW, anglePAWD, anglePDWA):
        self.angleWW = angleWW
        self.anglePAWD = anglePAWD
        self.anglePDWA = anglePDWA


class HydrogenBond:
    def __init__(self, donor, acceptor):
        self.donor = donor
        self.acceptor = acceptor

    @staticmethod
    def checkIsHBond(donor, acceptor, constraints):
        if donor.type != ResType.WATER and donor.atom.getName()[0] not in constraints.donors:
            return False
        if acceptor.type != ResType.WATER and acceptor.atom.getName()[0] not in constraints.acceptors:
            return False

        angleRange = ()
        if donor.type == ResType.WATER and acceptor.type == ResType.WATER:
            angleRange = constraints.angleWW
        elif donor.type == ResType.WATER and acceptor.type == ResType.PROTEIN:
            angleRange = constraints.anglePAWD
        else:
            angleRange = constraints.anglePDWA

        if not angleRange:
            return True

        return HydrogenBond.checkIsHBondAngle(donor, acceptor, angleRange)

    @staticmethod
    def checkIsHBondAngle(donor, acceptor, angleRange):
        for hydrogen in donor.hydrogens:
            bondAngle = calcAngle(donor.atom, hydrogen, acceptor.atom)
            if angleRange[0] < bondAngle < angleRange[1]:
                return True

        return False


def calcBridges(relations, hydrophilicList, method, maxDepth, maxNumResidues):
    waterBridges = []

    for atom in hydrophilicList:
        observedNode = relations[atom]
        if not observedNode:
            continue

        newBridges = []
        if method == 'chain':
            newBridges = getBridgeChain_BFS(
                observedNode, relations, maxDepth+1)
            relations.resetVisits()
        else:
            newBridges = getBridgeForResidue_BFS(
                observedNode, relations, maxDepth, maxNumResidues)

        waterBridges += newBridges

    return waterBridges


def getChainBridgeTuple(bridge):
    i_1, i_2 = bridge[0].getIndex(), bridge[-1].getIndex()
    return (min(i_1, i_2), max(i_1, i_2))


def getUniqueElements(list, key):
    unique, uniqueKeys = [], []
    for element in list:
        elementKey = key(element)
        if elementKey in uniqueKeys:
            continue

        unique.append(element)
        uniqueKeys.append(elementKey)

    return unique


def getBridgeChain_BFS(observedNode, relationsList, maxDepth):
    queue = deque([(observedNode.atom, [])])
    waterBridges = []

    while queue:
        currentAtom, currentPath = queue.popleft()
        currentNode = relationsList[currentAtom]

        if currentNode.isVisited:
            continue

        currentNode.isVisited = True

        if currentNode.type == ResType.PROTEIN and len(currentPath):
            waterBridges.append(currentPath + [currentAtom])
            continue

        if len(currentPath) == maxDepth:
            continue

        for atom in currentNode.bondedAtoms:
            queue.append(
                (atom, currentPath + [currentAtom]))

    return waterBridges


def getBridgeForResidue_BFS(observedNode, relationsList, maxDepth, maxNumResidues):
    waterBridges = []
    observedRelNode = relationsList[observedNode.atom]

    for waterAtom in observedRelNode.bondedAtoms:
        bridgeAtoms = []
        newWaters = []
        queue = deque([(waterAtom, 0)])
        while queue:
            currentAtom, depth = queue.popleft()
            currentNode = relationsList[currentAtom]

            if currentNode.isVisited or (maxDepth and depth > maxDepth):
                continue

            bridgeAtoms.append(currentAtom)

            if currentNode.type == ResType.PROTEIN:
                continue
            if currentNode.type == ResType.WATER:
                currentNode.isVisited = True
                newWaters.append(currentNode.atom)

            for atom in currentNode.bondedAtoms:
                queue.append((atom, depth+1))

        bridgeAtomIndices = list(
            set(map(lambda a: a.getIndex(), bridgeAtoms)))

        numProteinResidues = sum(
            relationsList[atomIndex].type == ResType.PROTEIN for atomIndex in bridgeAtomIndices)
        numResidues = len(bridgeAtomIndices)
        if (not maxNumResidues or numResidues <= maxNumResidues) and numProteinResidues >= 2:
            waterBridges.append(bridgeAtomIndices)
        else:
            for water in newWaters:
                relationsList[water].isVisited = False

    return waterBridges


def getInfoOutput(waterBridges, relations):
    output = []
    for bridge in waterBridges:
        bridgeOutput = []
        proteinAtoms, waterAtoms = [], []

        for atomIndex in bridge:
            if relations[atomIndex].type == ResType.PROTEIN:
                proteinAtoms.append(relations[atomIndex].atom)
            else:
                waterAtoms.append(relations[atomIndex].atom)

        for atom in proteinAtoms:
            residueInfo = f"{atom.getResname()}{atom.getResnum()}"
            atomInfo = f"{atom.getName()}_{atom.getIndex()}"
            chainInfo = atom.getChid()
            bridgeOutput += [residueInfo, atomInfo, chainInfo]

        for atom_1, atom_2 in combinations(proteinAtoms, r=2):
            bridgeOutput += [calcDistance(atom_1, atom_2)]

        bridgeOutput += [len(waterAtoms)]
        bridgeOutput += [
            list(map(lambda w: f"{w.getChid()}_{w.getIndex()}", waterAtoms))]

        output.append(bridgeOutput)

    return output


def getAtomicOutput(waterBridges, relations):
    output = []
    for bridge in waterBridges:
        proteinAtoms, waterAtoms = [], []
        for atomIndex in bridge:
            if relations[atomIndex].type == ResType.PROTEIN:
                proteinAtoms.append(relations[atomIndex].atom)
            else:
                waterAtoms.append(relations[atomIndex].atom)

        output.append((proteinAtoms, waterAtoms))

    return output


def calcWaterBridges(atoms, **kwargs):
    """Compute water bridges for a protein that has water molecules.

    :arg atoms: Atomic object from which atoms are considered
    :type atoms: :class:`.Atomic`

    :arg method: 'cluster' | 'chain'
      default is 'chain'
    :type method: string

    :arg distDA: maximal distance between water/protein donor and acceptor
        default is 3.5
    :type distDA: int, float

    :arg distWR: maximal distance between water and residue
        default is 4
    :type distWR: int, float

    :arg anglePDWA: angle range where protein is donor and water is acceptor
        default is (100, 200)
    :type anglePDWA: (int, int)

    :arg anglePAWD: angle range where protein is acceptor and water is donor
        default is (100, 140)
    :type anglePDWA: (int, int)

    :arg angleWW: angle between water donor/acceptor
        default is (140, 180)
    :type angleWW: (int, int)

    :arg maxDepth: maximum number of waters in chain/depth of residues in cluster
      default is 2
    :type maxDepth: int, None

    :arg maxNumRes: maximum number of water+protein residues in cluser
      default is None
    :type maxNumRes: int, None

    :arg donors: which atoms to count as donors 
        default is ['N', 'O', 'S', 'F']
    :type donors: list

    :arg acceptors: which atoms to count as acceptors 
        default is ['N', 'O', 'S', 'F']
    :type acceptors: list

    :arg output: output information arrays, (protein atoms, water atoms) or just atom indices per bridge
      default is 'info'
    :type output: 'info' | 'atomic' | 'indices'

    :filenamePDB: save residues and atoms that are forming water bridges into a PDB file
        default is None
    :type filenamePDB: string, None
    """

    method = kwargs.pop('method', 'chain')
    distDA = kwargs.pop('distDA', 3.5)
    distWR = kwargs.pop('distWR', 4)
    anglePDWA = kwargs.pop('anglePDWA', (100, 200))
    anglePAWD = kwargs.pop('anglePAWD', (100, 140))
    angleWW = kwargs.pop('angleWW', (140, 180))
    maxDepth = kwargs.pop('maxDepth', 2)
    maxNumResidues = kwargs.pop('maxNumRes', None)
    donors = kwargs.pop('donors', ['nitrogen', 'oxygen', 'sulfur'])
    acceptors = kwargs.pop('acceptors', ['nitrogen', 'oxygen', 'sulfur'])
    outputType = kwargs.pop('output', 'info')

    if method not in ['chain', 'cluster']:
        raise TypeError('Method should be chain or cluster.')

    relations = RelationList(len(atoms))
    consideredAtoms = ~atoms.select(
        f'water and not within {distWR} of protein')

    waterHydrogens = consideredAtoms.select('water and hydrogen') or []
    waterOxygens = consideredAtoms.select('water and oxygen')
    waterHydroOxyPairs = findNeighbors(waterOxygens, 1.4, waterHydrogens)
    for oxygen in waterOxygens:
        relations.addNode(oxygen, ResType.WATER)
    for pair in waterHydroOxyPairs:
        oxygen, hydrogen = pair[0], pair[1]
        relations[oxygen].hydrogens.append(hydrogen)

    proteinHydrophilic = consideredAtoms.select(
        f'protein and ({" or ".join(donors + acceptors)}) and within {distWR} of water')
    proteinHydrogens = consideredAtoms.select(f'protein and hydrogen') or []
    proteinHydroPairs = findNeighbors(
        proteinHydrophilic, 1.4, proteinHydrogens)
    for hydrophilic in proteinHydrophilic:
        relations.addNode(hydrophilic, ResType.PROTEIN)
    for pair in proteinHydroPairs:
        hydrophilic, hydrogen = pair[0], pair[1]
        relations[hydrophilic].hydrogens.append(hydrogen)

    contactingWaters = findNeighbors(waterOxygens, distDA)
    contactingWaterProtein = findNeighbors(
        waterOxygens, distDA, proteinHydrophilic)

    contactingWaters = list(
        map(lambda ww: (relations[ww[0]], relations[ww[1]]), contactingWaters))
    contactingWaterProtein = list(
        map(lambda wp: (relations[wp[0]], relations[wp[1]]), contactingWaterProtein))

    constraints = HBondConstraints(['N', 'O', 'S', 'F'], ['N', 'O', 'S', 'F'])
    if len(waterHydrogens) + len(proteinHydrogens):
        constraints.addAngles(angleWW, anglePAWD, anglePDWA)
    else:
        LOGGER.info('No hydrogens detected, angle criteria will not be used.')

    for pair in contactingWaters + contactingWaterProtein:
        for a, b in [(0, 1), (1, 0)]:
            if HydrogenBond.checkIsHBond(pair[a], pair[b], constraints):
                newHBond = HydrogenBond(pair[a].atom, pair[b].atom)
                relations.addHBond(newHBond)

    relations.removeUnnecessary()
    waterBridges = []

    waterBridges = calcBridges(
        relations, proteinHydrophilic, method, maxDepth, maxNumResidues)
    if method == 'chain':
        waterBridges = getUniqueElements(waterBridges, getChainBridgeTuple)

    if outputType == 'info':
        output = getInfoOutput(waterBridges, relations)
    elif outputType == 'atomic':
        output = getAtomicOutput(waterBridges, relations)
    else:
        output = waterBridges

    return output


# took from interactions.py
def calcWaterBridgesTrajectory(atoms, trajectory, **kwargs):
    """Compute selected type interactions for DCD trajectory or multi-model PDB 
    using default parameters."""

    interactions_all = []
    start_frame = kwargs.pop('start_frame', 0)
    stop_frame = kwargs.pop('stop_frame', -1)

    if trajectory is not None:
        if isinstance(trajectory, Atomic):
            trajectory = Ensemble(trajectory)

        # nfi = trajectory._nfi
        # trajectory.reset()
        numFrames = trajectory._n_csets

        if stop_frame == -1:
            traj = trajectory[start_frame:]
        else:
            traj = trajectory[start_frame:stop_frame+1]

        atoms_copy = atoms.copy()
        for j0, frame0 in enumerate(traj, start=start_frame):
            LOGGER.info('Frame: {0}'.format(j0))
            atoms_copy.setCoords(frame0.getCoords())
            interactions = calcWaterBridges(atoms_copy, **kwargs)
            interactions_all.append(interactions)
        # trajectory._nfi = nfi

    else:
        if atoms.numCoordsets() > 1:
            for i in range(len(atoms.getCoordsets()[start_frame:stop_frame])):
                LOGGER.info('Model: {0}'.format(i+start_frame))
                atoms.setACSIndex(i+start_frame)
                interactions = calcWaterBridges(atoms, **kwargs)
                interactions_all.append(interactions)
        else:
            LOGGER.info('Include trajectory or use multi-model PDB file.')

    return interactions_all


def calcWaterBridgesStatistics(frames, numAtoms):
    """Returns statistics - lil_matrix where indices are atom indices and value is percentage of bridge appearance of frames for each residue.

    :arg frames: list of water bridges from calcWaterBridgesTrajectory(), output='atomic'
    :type frames: list

    :arg numAtoms: list of atoms in PDB struct (also size of lil_matrix)
    :type data: list
    """
    interactionCount = lil_matrix((numAtoms, numAtoms), dtype=float)
    distanceSum = lil_matrix((numAtoms, numAtoms), dtype=float)

    for frame in frames:
        for bridge in frame:
            proteinAtoms = bridge[0]
            for atom_1, atom_2 in combinations(proteinAtoms, r=2):
                ind_1, ind_2 = atom_1.getIndex(), atom_2.getIndex()

                interactionCount[ind_1, ind_2] += 1
                interactionCount[ind_2, ind_1] += 1

                distance = calcDistance(atom_1, atom_2)
                distanceSum[ind_1, ind_2] += distance
                distanceSum[ind_2, ind_1] += distance

    interactionPerc = lil_matrix((numAtoms, numAtoms), dtype=float)
    distanceAvg = lil_matrix((numAtoms, numAtoms), dtype=float)

    for x, y in zip(*interactionCount.nonzero()):
        if not interactionCount[x, y]:
            continue

        interactionPerc[x, y] = interactionCount[x, y]/len(frames)
        distanceAvg[x, y] = distanceSum[x, y]/interactionCount[x, y]

    return {
        "interactionPercentage": interactionPerc,
        "distanceAverage": distanceAvg,
    }


def reduceTo1D(list, elementSel=lambda x: x, sublistSel=lambda x: x):
    return [elementSel(element) for sublist in list for element in sublistSel(sublist)]


def calcWaterBridgingResidues(frames, atoms, **kwargs):
    proteinThreshold = kwargs.pop('proteinThreshold', 0.7) * len(frames)
    waterThreshold = kwargs.pop('waterThreshold', 0.7) * len(frames)

    proteinAtoms, waterAtoms = np.zeros(len(atoms)), np.zeros(len(atoms))
    for i, frame in enumerate(frames):
        frameProteins = set(reduceTo1D(
            frame, lambda p: p.getIndex(), lambda wb: wb[0]))
        frameWaters = set(reduceTo1D(
            frame, lambda w: w.getIndex(), lambda wb: wb[1]))

        for atomIndex in frameProteins:
            proteinAtoms[atomIndex] += 1
        for atomIndex in frameWaters:
            waterAtoms[atomIndex] += 1

    bridgingProteinIndices = list(filter(
        lambda i: proteinAtoms[i] >= proteinThreshold, range(len(atoms))))
    bridgingWaterIndices = list(filter(
        lambda i: waterAtoms[i] >= waterThreshold, range(len(atoms))))

    bridgingProteinResidues = atoms.select(
        f'same residue as index {" ".join(map(str, bridgingProteinIndices))}')
    bridgingWaterResidues = atoms.select(
        f'same residue as index {" ".join(map(str, bridgingWaterIndices))}')

    if len(bridgingProteinIndices) == 0:
        bridgingProteinResidues = []
    if len(bridgingWaterIndices) == 0:
        bridgingWaterResidues = []

    return bridgingProteinResidues, bridgingWaterResidues


def savePDBWaterBridges(frames, atoms, filename, **kwargs):
    proteinResidues, waterResidues = calcWaterBridgingResidues(
        frames, atoms, **kwargs)

    atoms.setOccupancies(0)
    for atom in proteinResidues:
        atoms[atom.getIndex()].setOccupancy(1)

    atomsToSave = (atoms.select('protein') | waterResidues).toAtomGroup()
    return writePDB(filename, atomsToSave)
