##############################################################
# GDAL wrapper for convenient raster data handling and processing
# John Truckenbrodt 2015
# last update 2015-12-05
##############################################################

"""
This is intended as a raster meta information handler with options for reading and writing raster data in a convenient manner by simplifying the numerous options provided
by the GDAL python binding.
Several functions are provided along with this module to directly modify the raster object in memory or directly write a newly created file to disk (without modifying the raster
object itself). Upon initializing a Raster object only metadata is loaded, the actual data can be, for example, loaded to memory by calling functions matrix or load.
"""
#todo: function to write data with the same metadata as a given file
#todo: documentation

import os
import re
import vector
import spatial
import numpy as np
from osgeo.gdalconst import *
from osgeo import gdal, ogr, osr
from ancillary import dissolve, crsConvert, finder, run
from envi import HDRobject, hdr
import subprocess as sp
from math import ceil, floor, sqrt

os.environ["GDAL_PAM_PROXY_DIR"] = "/tmp"

gdal.UseExceptions()


class Raster(object):
    #todo: init a Raster object from array data not only from a filename
    def __init__(self, filename):
        if os.path.isfile(filename):
            self.filename = filename if os.path.isabs(filename) else os.path.join(os.getcwd(), filename)
            self.raster = gdal.Open(filename, GA_ReadOnly)
        else:
            raise IOError("file does not exist")

        self.cols = self.raster.RasterXSize
        self.rows = self.raster.RasterYSize
        self.bands = self.raster.RasterCount
        self.dim = [self.rows, self.cols, self.bands]
        self.driver = self.raster.GetDriver()
        self.format = self.driver.ShortName
        self.dtype = gdal.GetDataTypeName(self.raster.GetRasterBand(1).DataType)
        self.projection = self.raster.GetProjection()
        self.srs = osr.SpatialReference(wkt=self.projection)
        self.proj4 = self.srs.ExportToProj4().strip()
        try:
            self.epsg = crsConvert(self.proj4, "epsg")
        except TypeError:
            self.epsg = None
        self.geogcs = self.srs.GetAttrValue("geogcs")
        self.projcs = self.srs.GetAttrValue("projcs") if self.srs.IsProjected() else None
        self.geo = dict(zip(["xmin", "xres", "rotation_x", "ymax", "rotation_y", "yres"], self.raster.GetGeoTransform()))

        # note: yres is negative!
        self.geo["xmax"] = self.geo["xmin"] + self.geo["xres"] * self.cols
        self.geo["ymin"] = self.geo["ymax"] + self.geo["yres"] * self.rows

        self.res = [abs(float(self.geo["xres"])), abs(float(self.geo["yres"]))]
        self.nodata = self.raster.GetRasterBand(1).GetNoDataValue()

        self.__data = []

    @property
    def proj4args(self):
        args = [x.split("=") for x in re.split("[+ ]*", self.proj4) if len(x) > 0]
        return dict([(x[0], None) if len(x) == 1 else tuple(x) for x in args])

    @property
    def allstats(self):
        statcollect = []
        for x in self.layers():
            try:
                stats = x.ComputeStatistics(False)
            except RuntimeError:
                stats = None
            statcollect.append(stats)
        return statcollect

    # assign an array to an existing Raster object
    def assign(self, array, dim="full"):
        self.__data = [array]
        if dim != "full":
            shape = array.shape
            if len(shape) == 2:
                self.bands = 1
                self.rows, self.cols = shape
            else:
                self.bands, self.rows, self.cols = shape

            # print shape
            # print self.cols, self.rows
            # print self.raster.RasterXSize, self.raster.RasterYSize

            self.dim = [self.rows, self.cols, self.bands]
            self.geo["xmin"] += dim[0] * self.geo["xres"]
            self.geo["ymax"] += dim[1] * self.geo["yres"]
            self.geo["xmax"] = self.geo["xmin"] + self.geo["xres"] * self.cols
            self.geo["ymin"] = self.geo["ymax"] + self.geo["yres"] * self.rows
            self.raster.SetGeoTransform([self.geo[x] for x in ["xmin", "xres", "rotation_x", "ymax", "rotation_y", "yres"]])

    def bbox(self, outname=None, format="ESRI Shapefile", overwrite=True):
        if outname is None:
            return spatial.bbox(self.geo, self.proj4)
        else:
            spatial.bbox(self.geo, self.proj4, outname=outname, format=format, overwrite=overwrite)

    # translate raster data type descriptions
    @staticmethod
    def dtypes(typestring):
        dictionary = {"Byte": GDT_Byte, "Int16": GDT_Int16, "UInt16": GDT_UInt16, "Int32": GDT_Int32, "UInt32": GDT_UInt32, "Float32": GDT_Float32, "Float64": GDT_Float64}
        return dictionary[typestring]

    # extract weighted average of pixels intersecting with a defined radius to a point
    # radius is a multiple of the pixel resolution
    def extract(self, px, py, radius=1, no_data=0):

        xres, yres = self.res

        hx = xres / 2.0
        hy = yres / 2.0

        xlim = float(xres * radius)
        ylim = float(yres * radius)

        # compute minimum x and y pixel coordinates
        xmin = int((px - self.geo["xmin"] - xlim) // xres)
        ymin = int((self.geo["ymax"] - py - xlim) // yres)

        xmin = xmin if xmin >= 0 else 0
        ymin = ymin if ymin >= 0 else 0

        # compute maximum x and y pixel coordinates
        xmax = int((px - self.geo["xmin"] + xlim) // xres) + 2
        ymax = int((self.geo["ymax"] - py + ylim) // yres) + 2

        xmax = xmax if xmax <= self.cols else self.cols
        ymax = ymax if ymax <= self.rows else self.rows

        # load array subset
        array = self.raster.GetRasterBand(1).ReadAsArray(xmin, ymin, xmax - xmin, ymax - ymin)

        sum = 0
        counter = 0
        weightsum = 0
        for x in range(xmin, xmax):
            for y in range(ymin, ymax):
                # check whether point is a valid image index
                val = array[y - ymin, x - xmin]
                if val != no_data:
                    # compute distances of pixel center coordinate to requested point

                    xc = x * xres + hx + self.geo["xmin"]
                    yc = self.geo["ymax"] - y * yres + hy

                    dx = abs(xc - px)
                    dy = abs(yc - py)

                    # check whether point lies within ellipse: if ((dx ** 2) / xlim ** 2) + ((dy ** 2) / ylim ** 2) <= 1
                    weight = sqrt(dx ** 2 + dy ** 2)
                    sum += val * weight
                    weightsum += weight
                    counter += 1

        if sum > 0:
            return sum/weightsum
        else:
            if counter > 0:
                return 0
            else:
                return no_data

    # get specific raster layer information objects
    def layers(self):
        return [self.raster.GetRasterBand(band) for band in range(1, self.bands + 1)]

    # load all raster data to arrays
    def load(self, dim="full"):
        dim = [0, 0, self.cols, self.rows] if dim == "full" else dim
        for i in range(1, self.bands + 1):
            self.__data.append(self.matrix(i, dim))

    # returns an array of a raster band
    def matrix(self, band=1, dim="full"):
        dim = [0, 0, self.cols, self.rows] if dim == "full" else dim
        if len(self.__data) >= band:
            return self.__data[band - 1][dim[1]:dim[3], dim[0]:dim[2]]
        else:
            return self.raster.GetRasterBand(band).ReadAsArray(*dim)

    # compute basic statistic measures from selected bands (provided by either single integer keys or a list of integers)
    # def getstat(self, statistic, bands="all"):
    #     statistics = {"min": 0, "max": 1, "mean": 2, "sdev": 3}
    #     if statistic not in statistics:
    #         raise IOError("statistic not supported")
    #     if type(bands) == int:
    #         return self.allstats[bands-1][statistics[statistic]]
    #     elif bands == "all":
    #         return [self.allstats[x-1][statistics[statistic]] for x in range(1, self.bands+1)]
    #     elif type(bands) == list:
    #         return [self.allstats[x-1][statistics[statistic]] for x in bands]

    # crop a raster object using another raster or extent object
    # if no name for an output file is provided, a list of pixel coordinates for cropping is returned
    # def crop(self, clipobject, outname=None):
    #     ext = Extent(self)
    #     inter = intersect(self, clipobject)
    #     if inter is None:
    #         raise IOError("no extent overlap")
    #     clip = [int(ceil((inter.left-ext.left)/self.res[0])), int(ceil((ext.top-inter.top)/self.res[1])),
    #             int(floor((inter.right-inter.left)/self.res[0])), int(floor((inter.top-inter.bottom)/self.res[1]))]
    #     if outname is not None:
    #         self.write(outname, dim=clip)
    #     else:
    #         return clip

    # remove all lines and columns containing only no data values
    def reduce(self, outname=None, format="ENVI"):

        if self.bands != 1:
            raise ValueError("only single band images supported")

        stats = self.allstats[0]

        if stats[0] == stats[1]:
            raise ValueError("file does not contain valid pixels")

        # load raster layer into an array
        mat = self.matrix()

        mask1 = ~np.all(mat == self.nodata, axis=0)
        mask2 = ~np.all(mat == self.nodata, axis=1)
        mask1_l = mask1.tolist()
        mask2_l = mask2.tolist()

        left = mask1_l.index(True)
        cols = len(mask1_l) - mask1_l[::-1].index(True) - left
        top = mask2_l.index(True)
        rows = len(mask2_l) - mask2_l[::-1].index(True) - top

        mat = mat[mask2, :]
        mat = mat[:, mask1]

        if outname is None:
            self.assign(mat, dim=[left, top, cols, rows])
        else:
            self.write(outname, dim=[left, top, cols, rows], format=format)

    # perform raster computations with custom functions and assign them to the exitsting raster object in memory
    def rescale(self, function):

        if self.bands != 1:
            raise ValueError("only single band images supported")

        # load array
        mat = self.matrix()

        # scale values
        scaled = function(mat)

        # round to nearest integer
        rounded = np.rint(scaled)

        # assign newly computed array to raster object
        self.assign(rounded)

    # write the raster object to a file
    # if the data itself has been loaded to self.data (by function load), the in-memory data will be written to the file, otherwise the data is copied from the source file
    # the parameter dim gives the opportunity to write a cropped version of the raster file; a dim-formatted list is, for example, returned by function crop
    def write(self, outname, dtype="default", format="ENVI", dim="full", overwrite=True):

        # if overwrite:
        #     for item in finder(os.path.basename(outname), [os.path.splitext(os.path.basename(outname))[0]], regex=True):
        #         os.remove(item)
        # else:
        #     raise RuntimeError("file already exists")

        if format == "GTiff" and not outname.endswith(".tif"):
            outname += ".tif"

        dtype = self.dtype if dtype == "default" else dtype

        geo = list(self.raster.GetGeoTransform())

        if dim != "full":
            geo[0] += dim[0] * geo[1]
            geo[3] += dim[1] * geo[5]

        dim = [0, 0, self.cols, self.rows] if dim == "full" else dim
        driver = gdal.GetDriverByName(format)
        outDataset = driver.Create(outname, dim[2], dim[3], self.bands, self.dtypes(dtype))
        if self.geo is not None:
            outDataset.SetGeoTransform(geo)
        if self.projection is not None:
            outDataset.SetProjection(self.projection)
        for i in range(1, self.bands + 1):
            outband = outDataset.GetRasterBand(i)
            outband.SetNoDataValue(self.nodata)
            mat = self.raster.GetRasterBand(i).ReadAsArray(*dim) if len(self.__data) == 0 else self.__data[i - 1]
            outband.WriteArray(mat)
            outband.FlushCache()
        outDataset = None

    # write a png image of three raster bands (provided in a list of 1-based integers); percent controls the size ratio of input and output
    # def png(self, bands, outname, percent=10):
    #     if len(bands) != 3 or max(bands) not in range(1, self.bands+1) or min(bands) not in range(1, self.bands+1):
    #         print "band indices out of range"
    #         return
    #     if not outname.endswith(".png"):
    #         outname += ".png"
    #     exp_bands = " ".join(["-b "+str(x) for x in bands]).split()
    #     exp_scale = [["-scale", self.getstat("min", x), self.getstat("max", x), 0, 255] for x in bands]
    #     exp_size = ["-outsize", str(percent)+"%", str(percent)+"%"]
    #     cmd = dissolve([["gdal_translate", "-q", "-of", "PNG", "-ot", "Byte"], exp_size, exp_bands, exp_scale, self.filename, outname])
    #     sp.check_call([str(x) for x in cmd])


# object containing the outer coordinates of a raster object as well as the enclosed area in square map units
# input can be a raster object or a list
class Extent(object):
    def __init__(self, geoobject):
        if type(geoobject) == Raster:
            gt = geoobject.geo
            self.proj4 = geoobject.proj4
            self.all = [gt["xmin"], gt["ymin"], gt["xmax"], gt["ymax"]]
        elif type(geoobject) == list:
            geoobject = [float(x) for x in geoobject]
            if geoobject[0] > geoobject[2] or geoobject[1] > geoobject[3]:
                raise ValueError("wrong order of elements; must be [xmin, ymin, xmax, ymax]")
            self.all = geoobject
        self.xmin, self.ymin, self.xmax, self.ymax = self.all
        self.area = abs(self.xmax - self.xmin) * abs(self.ymax - self.ymin)


# def init(rasterobject, outname):
#     rows, cols, bands = rasterobject.dim
#     outDataset = rasterobject.driver.Create(outname, cols, rows, bands, rasterobject.dtypes(rasterobject.dtype))
#     if rasterobject.geotransform is not None:
#         outDataset.SetGeoTransform(rasterobject.raster.GetGeoTransform())
#     if rasterobject.projection is not None:
#         outDataset.SetProjection(rasterobject.projection)
#     return outDataset

# compute the geographical intersection between two objects of type Raster or Extent
def intersect(obj1, obj2):
    if type(obj1) == Raster:
        ext1 = Extent(obj1)
        proj1 = obj1.proj4
    elif type(obj1) == Extent:
        ext1 = obj1
        if hasattr(obj1, "proj4"):
            proj1 = obj1.proj4
    else:
        raise IOError("type Raster or Extent expected as first argument")
    if type(obj2) == Raster:
        ext2 = Extent(obj2)
        proj2 = obj2.proj4
    elif type(obj2) == Extent:
        ext2 = obj2
        if hasattr(obj2, "proj4"):
            proj2 = obj2.proj4
    else:
        raise IOError("type Raster or Extent expected as second argument")
    # if proj1 != proj2:
    #     raise IOError("different projections")
    try:
        intersection = Extent([max(ext1.xmin, ext2.xmin), max(ext1.ymin, ext2.ymin), min(ext1.xmax, ext2.xmax), min(ext1.ymax, ext2.ymax)])
        # intersection.proj4 = proj1
        return intersection
    except ValueError:
        return None


# reproject a raster file
def reproject(rasterobject, reference, outname, resampling="bilinear", format="ENVI"):
    rasterobject = rasterobject if type(rasterobject) == Raster else Raster(rasterobject)
    projection = reference.projection if type(reference) == Raster else reference
    sp.check_call(["gdalwarp", "-overwrite", "-q", "-r", resampling, "-of", format,
                   "-tr", str(rasterobject.res[0]), str(rasterobject.res[1]),
                   "-srcnodata", str(rasterobject.nodata), "-dstnodata", str(rasterobject.nodata),
                   "-t_srs", projection, rasterobject.filename, outname])


# stack multiple raster objects into one ENVI file
# def stack(rasters, outname, maxmem=10000):
#     if len(set([x.cols for x in rasters])) > 1 or len(set([x.rows for x in rasters])) > 1:
#         raise IOError("dimension mismatch")
#     if len(set(["".join([str(y) for y in x.raster.GetGeoTransform()]) for x in rasters])) > 1:
#         raise IOError("location mismatch")
#     if len(set([x.projection for x in rasters])) > 1:
#         raise IOError("projection mismatch")
#
#     ref = rasters[0]
#     cols = ref.cols
#     rows = ref.rows
#     bands = sum([x.bands for x in rasters])
#     maxlines = maxmem // (cols * bands * 4 / 1048576.)
#
#     driver = gdal.GetDriverByName("ENVI")
#     out = driver.Create(outname, cols, rows, bands, GDT_Float32)
#
#     index = 0
#     bandnames = []
#     while index < rows:
#         max = rows if index + maxlines > rows else index + maxlines
#
#         outarray = np.zeros((bands, max - index, cols), dtype=np.float32)
#         bandindex = 0
#         for item in rasters:
#
#             hdrfile = os.path.splitext(item.filename)[0] + ".hdr"
#             if os.path.isfile(hdrfile):
#                 names = HDRobject(hdrfile).band_names
#
#                 for name in names:
#                     if re.search("Band [0-9]*", name):
#                         bandnames.append(os.path.splitext(os.path.basename(item.filename))[0] + ": " + name)
#                     else:
#                         bandnames.append(name)
#             else:
#                 bandnames.append(os.path.splitext(os.path.basename(item.filename))[0])
#
#             outarray[bandindex:bandindex + item.bands, :, :] = item.raster.ReadAsArray(0, index, cols, max)
#             bandindex += item.bands
#         for band in range(1, bands + 1):
#             maskout = out.GetRasterBand(band)
#             maskout.WriteArray(outarray[band - 1, :, :], index, 0)
#             maskout.FlushCache()
#         index += max
#
#     out.SetGeoTransform(ref.raster.GetGeoTransform())
#     out.SetProjection(ref.raster.GetProjection())
#
#     out = None
#
#     hdrfile = os.path.splitext(outname)[0] + ".hdr"
#     header = HDRobject(hdrfile)
#     header.band_names = bandnames
#     hdr(header)

def stack(srcfiles, dstfile, resampling, targetres, srcnodata, dstnodata, shapefile=None, layernames=None, sortfun=None):

    if layernames is not None:
        if len(layernames) != len(srcfiles):
            raise IOError("mismatch between number of source file groups and layernames")

    if not isinstance(targetres, list) and len(targetres) != 2:
        raise IOError("targetres must be a list with two entries for x and y resolution")

    if len(srcfiles) == 1:
        raise IOError("only one file specified; nothing to be done")

    if resampling not in ["near", "bilinear", "cubic", "cubicspline", "lanczos", "average", "mode",  "max", "min", "med", "Q1", "Q3"]:
        raise IOError("resampling method not supported")

    projections = list(set([Raster(x).projection for x in dissolve(srcfiles)]))
    if len(projections) > 1:
        raise IOError("raster projection mismatch")
    else:
        srs = projections[0]

    # read shapefile bounding coordinates and reduce list of rasters to those overlapping with the shapefile
    if shapefile is not None:
        shp = vector.Vector(shapefile)
        shp.reproject(srs)
        ext = shp.extent
        arg_ext = ["-te", ext["xmin"], ext["ymin"], ext["xmax"], ext["ymax"]]

        for i in range(len(srcfiles)):
            group = sorted(srcfiles[i], key=sortfun) if isinstance(srcfiles[i], list) else [srcfiles[i]]
            group = [x for x in group if spatial.intersect(shp, Raster(x))]
            if len(group) > 1:
                srcfiles[i] = group
            elif len(group) == 1:
                srcfiles[i] = group[0]
            else:
                srcfiles[i] = None
        srcfiles = [x for x in srcfiles if x is not None]
    else:
        arg_ext = []

    # define warping arguments
    arg_targetres = dissolve(["-tr", targetres]) if targetres is not None else []
    arg_srcnodata = ["-srcnodata", srcnodata] if srcnodata is not None else []
    arg_dstnodata = ["-dstnodata", dstnodata] if dstnodata is not None else []
    arg_resampling = ["-r", resampling] if resampling is not None else []
    arg_format = ["-of", "ENVI"]

    # create VRT files for mosaicing
    vrtlist = []
    for i in range(len(srcfiles)):
        if isinstance(srcfiles[i], list):
            vrt = os.path.join(os.path.dirname(dstfile), os.path.splitext(os.path.basename(srcfiles[i][0]))[0]+".vrt")
            run(["gdalbuildvrt", "-overwrite", arg_srcnodata, arg_ext, vrt, srcfiles[i]])
            srcfiles[i] = vrt
            vrtlist.append(vrt)

    # if no specific layernames are defined and sortfun is not set to None, sort files by custom function or, by default, the basename of the raster/VRT file
    if layernames is None and sortfun is not None:
        srcfiles = sorted(srcfiles, key=sortfun if sortfun else os.path.basename)

    # create VRT for stacking
    vrt = os.path.splitext(dstfile)[0]+".vrt"
    run(["gdalbuildvrt", "-q", "-overwrite", "-separate", arg_srcnodata, arg_ext, vrt, srcfiles])
    vrtlist.append(vrt)

    # warp files
    run(["gdalwarp", "-q", "-multi", "-overwrite", arg_resampling, arg_format, arg_srcnodata, arg_dstnodata, arg_targetres, vrt, dstfile])
    # ["--config", "GDAL_CACHEMAX", 2000, "-wm", 6000, "-co", "INTERLEAVE="+interleave]

    # remove VRT files
    for vrt in vrtlist:
        os.remove(vrt)

    # edit ENVI HDR files to contain specific layer names
    par = HDRobject(dstfile+".hdr")
    par.band_names = [os.path.splitext(os.path.basename(x))[0] for x in srcfiles] if layernames is None else layernames
    hdr(par)