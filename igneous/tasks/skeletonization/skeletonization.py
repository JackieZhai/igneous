"""
Skeletonization algorithm based on TEASAR (Sato et al. 2000).

Authors: Alex Bae and Will Silversmith
Affiliation: Seung Lab, Princeton Neuroscience Institue
Date: June-August 2018
"""
from collections import defaultdict
from math import log

import edt
import numpy as np
from scipy import ndimage
from PIL import Image

import igneous.dijkstra 
import igneous.skeletontricks

from cloudvolume import PrecomputedSkeleton
from cloudvolume.lib import save_images, mkdir

def TEASAR(
    labels, DBF, 
    scale=10, const=10, anisotropy=(1,1,1), 
    soma_detection_threshold=5000, 
    pdrf_scale=5000, pdrf_exponent=16,
    soma_invalidation_scale=0.5,
    soma_invalidation_const=0,
    path_downsample=1
  ):
  """
  Given the euclidean distance transform of a label ("Distance to Boundary Function"), 
  convert it into a skeleton with scale and const TEASAR parameters. 

  DBF: Result of the euclidean distance transform. Must represent a single label,
       assumed to be expressed in chosen physical units (i.e. nm)
  scale: during the "rolling ball" invalidation phase, multiply the DBF value by this.
  const: during the "rolling ball" invalidation phase, this is the minimum radius in chosen physical units (i.e. nm).
  anisotropy: (x,y,z) conversion factor for voxels to chosen physical units (i.e. nm)
  soma_detection_threshold: if object has a DBF value larger than this, 
    root will be placed at largest DBF value and special one time invalidation
    will be run over that root location (see soma_invalidation scale)
    expressed in chosen physical units (i.e. nm) 
  pdrf_scale: scale factor in front of dbf, used to weight dbf over euclidean distance (higher to pay more attention to dbf) (default 5000)
  pdrf_exponent: exponent in dbf formula on distance from edge, faster if factor of 2 (default 16)
  soma_invalidation_scale: the 'scale' factor used in the one time soma root invalidation (default .5)
  soma_invalidation_const: the 'const' factor used in the one time soma root invalidation (default 0)
                           (units in chosen physical units (i.e. nm))
  path_downsample: stride length for downsampling the saved skeleton paths (default 1)
                    (units of node points)
  
  Based on the algorithm by:

  M. Sato, I. Bitter, M. Bender, A. Kaufman, and M. Nakajima. 
  "TEASAR: tree-structure extraction algorithm for accurate and robust skeletons"  
    Proc. the Eighth Pacific Conference on Computer Graphics and Applications. Oct. 2000.
    doi:10.1109/PCCGA.2000.883951 (https://ieeexplore.ieee.org/document/883951/)

  Returns: Skeleton object
  """
  dbf_max = np.max(DBF)
  labels = np.asfortranarray(labels)
  DBF = np.asfortranarray(DBF)

  soma_mode = dbf_max > soma_detection_threshold
  # > 5000 nm, gonna be a soma or blood vessel
  # For somata: specially handle the root by 
  # placing it at the approximate center of the soma
  if soma_mode:
    labels = ndimage.binary_fill_holes(labels)
    DBF = edt.edt(np.ascontiguousarray(labels), anisotropy=anisotropy)
    DBF = np.asfortranarray(DBF)
    labels = np.asfortranarray(labels)
    dbf_max = np.max(DBF)
    root = np.unravel_index(np.argmax(DBF), DBF.shape)
    soma_radius = dbf_max * soma_invalidation_scale + soma_invalidation_const
  else:
    root = find_root(labels, anisotropy)
    soma_radius = 0.0

  if root is None:
    return PrecomputedSkeleton()
 
  DAF = igneous.dijkstra.euclidean_distance_field(labels, root, anisotropy=anisotropy)
  PDRF = compute_pdrf(dbf_max, pdrf_scale, pdrf_exponent, DBF, DAF)
  del DAF

  # Use dijkstra propogation w/o a target to generate a field of
  # pointers from each voxel to its parent. Then we can rapidly
  # compute multiple paths by simply hopping pointers using path_from_parents
  parents = igneous.dijkstra.parental_field(PDRF, root)

  if soma_mode:
    invalidated, labels = igneous.skeletontricks.roll_invalidation_ball(
      labels, DBF, np.array([root], dtype=np.uint32),
      scale=soma_invalidation_scale,
      const=soma_invalidation_const, 
      anisotropy=anisotropy
    )

  paths = compute_paths(
    root, labels, DBF, PDRF, 
    parents, scale, const, anisotropy, 
    soma_mode, soma_radius
  )

  # Downsample skeletons by striding. Ensure first and last point remain.
  # Duplicate points are eliminated in path_union.
  for i, path in enumerate(paths):
    paths[i] = np.concatenate(
      (path[0:-2:path_downsample, :], path[-1:, :])
    )

  skel_verts, skel_edges = path_union(paths)
  skel_radii = DBF[skel_verts[::3], skel_verts[1::3], skel_verts[2::3]]

  skel_verts = skel_verts.astype(np.float32).reshape( (skel_verts.size // 3, 3) )
  skel_edges = skel_edges.reshape( (skel_edges.size // 2, 2)  )

  return PrecomputedSkeleton(skel_verts, skel_edges, skel_radii)

def compute_paths(
    root, labels, DBF, PDRF, 
    parents, scale, const, anisotropy, 
    soma_mode, soma_radius
  ):
  """
  Given the labels, DBF, PDRF, dijkstra parents,
  and associated invalidation knobs, find the set of paths 
  that cover the object. Somas are given special treatment
  in that we attempt to cull vertices within a radius of the
  root vertex.
  """
  invalid_vertices = {}

  if soma_mode:
    invalid_vertices[root] = True

  paths = []
  valid_labels = np.count_nonzero(labels)
    
  while valid_labels > 0:
    target = igneous.skeletontricks.find_target(labels, PDRF)
    path = igneous.dijkstra.path_from_parents(parents, target)
    
    if soma_mode:
      dist_to_soma_root = np.linalg.norm(anisotropy * (path - root), axis=1)
      # remove all path points which are within soma_radius of root
      path = np.concatenate(
        (path[:1,:], path[dist_to_soma_root > soma_radius, :])
      )

    invalidated, labels = igneous.skeletontricks.roll_invalidation_cube(
      labels, DBF, path, scale, const, 
      anisotropy=anisotropy, invalid_vertices=invalid_vertices,
    )

    valid_labels -= invalidated
    for vertex in path:
      invalid_vertices[tuple(vertex)] = True

    paths.append(path)

  return paths

def find_root(labels, anisotropy):
  """
  "4.4 DAF:  Compute distance from any voxel field"
  Compute DAF, but we immediately convert to the PDRF
  The extremal point of the PDRF is a valid root node
  even if the DAF is computed from an arbitrary pixel.
  """
  any_voxel = igneous.skeletontricks.first_label(labels)   
  if any_voxel is None: 
    return None

  DAF = igneous.dijkstra.euclidean_distance_field(
    np.asfortranarray(labels), any_voxel, anisotropy=anisotropy)
  return igneous.skeletontricks.find_target(labels, DAF)

def is_power_of_two(num):
  if int(num) != num:
    return False
  return num != 0 and ((num & (num - 1)) == 0)

def compute_pdrf(dbf_max, pdrf_scale, pdrf_exponent, DBF, DAF):
  """
  Add p(v) to the DAF (pp. 4, section 4.5)
  "4.5 PDRF: Compute penalized distance from root voxel field"
  Let M > max(DBF)
  p(v) = 5000 * (1 - DBF(v) / M)^16
  5000 is chosen to allow skeleton segments to be up to 3000 voxels
  long without exceeding floating point precision.

  IMPLEMENTATION NOTE: 
  Appearently repeated *= is much faster than "** f(16)" 
  12,740.0 microseconds vs 4 x 560 = 2,240 microseconds (5.69x)

  More clearly written:
  PDRF = DAF + 5000 * ((1 - DBF * M) ** 16)
  """
  f = lambda x: np.float32(x)
  M = f( 1 / (dbf_max ** 1.01) )

  if is_power_of_two(pdrf_exponent) and (pdrf_exponent < (2 ** 16)):
    PDRF = (f(1) - (DBF * M)) # ^1
    for _ in range(int(np.log2(pdrf_exponent))):
      PDRF *= PDRF # ^pdrf_exponent
  else: 
    PDRF = (f(1) - (DBF * M)) ** pdrf_exponent

  PDRF *= f(pdrf_scale)
  PDRF += DAF

  return np.asfortranarray(PDRF)

def path_union(paths):
  """
  Given a set of paths with a common root, attempt to join them
  into a tree at the first common linkage.
  """
  if len(paths) == 0:
    npv = np.zeros((0,), dtype=np.uint32)
    npe = np.zeros((0,), dtype=np.uint32)
    return npv, npe

  tree = defaultdict(set)
  tree_id = {}
  vertices = []

  ct = 0
  for path in paths:
    for i in range(path.shape[0] - 1):
      parent = tuple(path[i, :].tolist())
      child = tuple(path[i + 1, :].tolist())
      tree[parent].add(child)
      if not parent in tree_id:
        tree_id[parent] = ct
        vertices.append(parent)
        ct += 1
      if not child in tree:
        tree[child] = set()
      if not child in tree_id:
        tree_id[child] = ct
        vertices.append(child)
        ct += 1 

  root = tuple(paths[0][0,:].tolist())
  edges = []

  # Note: Chose iterative rather than recursive solution
  # because somas can cause stack overflows for small TEASAR
  # parameters.
  stack = [ root ]

  while len(stack) > 0:
    parent = stack.pop()
    for child in tree[parent]:
      edges.append([ tree_id[parent], tree_id[child] ])
      stack.append(child)

  npv = np.zeros((len(vertices) * 3,), dtype=np.uint32)
  for i, vertex in enumerate(vertices):
    npv[ 3 * i + 0 ] = vertex[0]
    npv[ 3 * i + 1 ] = vertex[1]
    npv[ 3 * i + 2 ] = vertex[2]

  npe = np.zeros((len(edges) * 2,), dtype=np.uint32)
  for i, edge in enumerate(edges):
    npe[ 2 * i + 0 ] = edges[i][0]
    npe[ 2 * i + 1 ] = edges[i][1]

  return npv, npe

def xy_path_projection(paths, labels, N=0):
  if type(paths) != list:
    paths = [ paths ]

  projection = np.zeros( (labels.shape[0], labels.shape[1] ), dtype=np.uint8)
  outline = labels.any(axis=-1).astype(np.uint8) * 77
  outline = outline.reshape( (labels.shape[0], labels.shape[1] ) )
  projection += outline
  for path in paths:
    for coord in path:
      projection[coord[0], coord[1]] = 255

  projection = Image.fromarray(projection.T, 'L')
  N = str(N).zfill(3)
  mkdir('./saved_images/projections')
  projection.save('./saved_images/projections/{}.png'.format(N), 'PNG')

