##############################################################
# Reading and Organizing system for SAR images
# John Truckenbrodt 2016
# last update 2016-03-18
##############################################################
"""
this script is intended to contain several SAR scene identifier classes to read basic metadata from the scene folders/files, convert to GAMMA format and do simple pre-processing
"""

import os
import re
import abc
import math
from osgeo import gdal
from osgeo.gdalconst import GA_ReadOnly
import spatial
import zipfile as zf
import tarfile as tf
from ancillary import finder, parse_literal, run
from time import strptime, strftime
import xml.etree.ElementTree as ElementTree


def identify(scene, mode="full"):
    """Return a metadata handler of the given scene."""
    for handler in ID.__subclasses__():
        try:
            return handler(scene, mode)
        except IOError:
            pass
    raise IOError("data format not supported")


class ID(object):
    """Abstract class for SAR meta data handlers."""

    def bbox(self, outname=None, overwrite=True):
        """Return the bounding box."""
        if outname is None:
            return spatial.bbox(self.getCorners(), self.projection)
        else:
            spatial.bbox(self.getCorners(), self.projection, outname=outname, format="ESRI Shapefile", overwrite=overwrite)

    @abc.abstractmethod
    def calibrate(self, replace=False):
        return

    @property
    def compression(self):
        if os.path.isdir(self.scene):
            return None
        elif zf.is_zipfile(self.scene):
            return "zip"
        elif tf.is_tarfile(self.scene):
            return "tar"
        else:
            return None

    @abc.abstractmethod
    def convert2gamma(self, directory):
        return

    def examine(self):
        files = self.findfiles(self.pattern)
        if len(files) == 1:
            self.file = files[0]
        elif len(files) == 0:
            raise IOError("folder does not match {} scene naming convention".format(type(self).__name__))
        else:
            raise IOError("file ambiguity detected")

    def findfiles(self, pattern):
        if os.path.isdir(self.scene):
            files = [self.scene] if re.search(pattern, os.path.basename(self.scene)) else finder(self.scene, [pattern], regex=True)
        elif zf.is_zipfile(self.scene):
            with zf.ZipFile(self.scene, "r") as zip:
                files = [os.path.join(self.scene, x.strip("/")) for x in zip.namelist() if re.search(pattern, x.strip("/"))]
        elif tf.is_tarfile(self.scene):
            tar = tf.open(self.scene)
            files = [os.path.join(self.scene, x) for x in tar.getnames() if re.search(pattern, x)]
            tar.close()
        else:
            files = [self.scene] if re.search(pattern, self.scene) else []
        return files

    def gdalinfo(self, scene):
        """

        Args:
            scene: an archive containing a SAR scene

        sets object attributes

        """
        self.scene = os.path.realpath(scene)
        files = self.findfiles("(?:\.[NE][12]$|DAT_01\.001$|product\.xml|manifest\.safe$)")

        if len(files) == 1:
            prefix = {"zip": "/vsizip/", "tar": "/vsitar/", None: ""}[self.compression]
            header = files[0]
        elif len(files) > 1:
            raise IOError("file ambiguity detected")
        else:
            raise IOError("file type not supported")

        ext_lookup = {".N1": "ASAR", ".E1": "ERS1", ".E2": "ERS2"}
        extension = os.path.splitext(header)[1]
        if extension in ext_lookup:
            self.sensor = ext_lookup[extension]

        img = gdal.Open(prefix+header, GA_ReadOnly)
        meta = img.GetMetadata()
        self.cols, self.rows, self.bands = img.RasterXSize, img.RasterYSize, img.RasterCount
        self.projection = img.GetGCPProjection()
        self.gcps = [((x.GCPPixel, x.GCPLine), (x.GCPX, x.GCPY, x.GCPZ)) for x in img.GetGCPs()]
        img = None

        for item in meta:
            entry = [item, parse_literal(meta[item].strip())]

            # todo: check module time for more general approaches
            for timeformat in ["%d-%b-%Y %H:%M:%S.%f", "%Y%m%d%H%M%S%f", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%fZ"]:
                try:
                    entry[1] = strftime("%Y%m%dT%H%M%S", strptime(entry[1], timeformat))
                except (TypeError, ValueError):
                    pass

            if re.search("(?:LAT|LONG)", entry[0]):
                entry[1] /= 1000000.
            setattr(self, entry[0], entry[1])

    @abc.abstractmethod
    def getCorners(self):
        return

    def getGammaImages(self, directory=None):
        if directory is None:
            if hasattr(self, "gammadir"):
                directory = self.gammadir
            else:
                raise IOError("directory missing; please provide directory to function or define object attribute 'gammadir'")
        return [x for x in finder(directory, [self.outname_base()], regex=True) if not re.search("\.(?:par|hdr|aux\.xml)$", x)]

    def getHGT(self):

        corners = self.getCorners()

        # generate sequence of integer coordinates marking the tie points of the overlapping hgt tiles
        lat = range(int(float(corners["ymin"])//1), int(float(corners["ymax"])//1)+1)
        lon = range(int(float(corners["xmin"])//1), int(float(corners["xmax"])//1)+1)

        # convert coordinates to string with leading zeros and hemisphere identification letter
        lat = [str(x).zfill(2+len(str(x))-len(str(x).strip("-"))) for x in lat]
        lat = [x.replace("-", "S") if "-" in x else "N"+x for x in lat]

        lon = [str(x).zfill(3+len(str(x))-len(str(x).strip("-"))) for x in lon]
        lon = [x.replace("-", "W") if "-" in x else "E"+x for x in lon]

        # concatenate all formatted latitudes and longitudes with each other as final product
        return [x+y+".hgt" for x in lat for y in lon]

    @abc.abstractmethod
    def outname_base(self):
        return

    def summary(self):
        for item in sorted(self.__dict__.keys()):
            if item != "gcps":
                print "{0}: {1}".format(item, getattr(self, item))

    @abc.abstractmethod
    def unpack(self, directory):
        return

    # todo: prevent unpacking if target files already exist
    def _unpack(self, directory):
        if not os.path.isdir(directory):
            os.makedirs(directory)
        if tf.is_tarfile(self.scene):
            archive = tf.open(self.scene, "r")
            names = archive.getnames()
            header = os.path.commonprefix(names)

            if header in names:
                if archive.getmember(header).isdir():
                    for item in sorted(names):
                        if item != header:
                            member = archive.getmember(item)
                            outname = os.path.join(directory, item.replace(header+"/", ""))
                            if member.isdir():
                                os.makedirs(outname)
                            else:
                                with open(outname, "w") as outfile:
                                    outfile.write(member.tobuf())
                    archive.close()
                else:
                    archive.extractall(directory)
                    archive.close()
        elif zf.is_zipfile(self.scene):
            archive = zf.ZipFile(self.scene, "r")
            names = archive.namelist()
            header = os.path.commonprefix(names)
            if header.endswith("/"):
                for item in sorted(names):
                    if item != header:
                        outname = os.path.join(directory, item.replace(header, ""))
                        if item.endswith("/"):
                            os.makedirs(outname)
                        else:
                            with open(outname, "w") as outfile:
                                outfile.write(archive.read(item))
                archive.close()
            else:
                archive.extractall(directory)
                archive.close()
        self.scene = directory
        self.file = os.path.join(self.scene, os.path.basename(self.file))


# class CEOS(ID):
#     # todo: What sensors other than ERS1, ERS2 and Envisat ASAR should be included?
#     # todo: add a pattern to check if the scene could be handled by CEOS
#     def __init__(self, scene):
#
#         raise IOError
#
#         self.gdalinfo(scene)
#         self.sensor = self.CEOS_MISSION_ID
#         self.start = self.CEOS_ACQUISITION_TIME
#         self.incidence = self.CEOS_INC_ANGLE
#         self.spacing = (self.CEOS_PIXEL_SPACING_METERS, self.CEOS_LINE_SPACING_METERS)
#
#         # todo: check whether this is correct:
#         self.orbit = "D" if self.CEOS_PLATFORM_HEADING > 180 else "A"
#         self.k_db = -10*math.log(self.CEOS_CALIBRATION_CONSTANT_K, 10)
#         self.sc_db = {"ERS1": 59.61, "ERS2": 60}[self.sensor]
#         self.outname_base = "{0}______{1}".format(*[self.sensor, self.start])
#
#     # todo: change coordinate extraction to the exact boundaries of the image (not outer pixel center points)
#     def getCorners(self):
#         lat = [x[1][1] for x in self.gcps]
#         lon = [x[1][0] for x in self.gcps]
#         return {"xmin": min(lon), "xmax": max(lon), "ymin": min(lat), "ymax": max(lat)}
#
#     def convert2gamma(self, directory):
#         if self.sensor in ["ERS1", "ERS2"]:
#             outname = os.path.join(directory, self.outname_base+"_VV_slc")
#             lea = os.path.join(self.scene, "LEA_01.001")
#             title = os.path.basename(self.findfiles("\.PS$")[0]).replace(".PS", "")
#             run(["par_ESA_ERS", lea, outname+".par", self.file, outname], inlist=[title])
#         else:
#             raise NotImplementedError("sensor {} not implemented yet".format(self.sensor))
#
#     def unpack(self, directory):
#         if self.sensor in ["ERS1", "ERS2"]:
#             outdir = os.path.join(directory, re.sub("\.[EN][12]\.PS$", "", os.path.basename(self.findfiles("\.PS$")[0])))
#             self._unpack(outdir)
#         else:
#             raise NotImplementedError("sensor {} not implemented yet".format(self.sensor))

# id = identify("/geonfs01_vol1/ve39vem/ERS/ERS1_0132_2529_20dec95")
# id = identify("/geonfs01_vol1/ve39vem/ERS/ERS1_0132_2529_20dec95.zip")


class ESA(ID):
    def __init__(self, scene, mode="full"):

        self.pattern = r"(?P<product_id>(?:SAR|ASA)_(?:IM(?:S|P|G|M|_)|AP(?:S|P|G|M|_)|WV(?:I|S|W|_))_[012B][CP])" \
                       r"(?P<processing_stage_flag>[A-Z])" \
                       r"(?P<originator_ID>[A-Z\-]{3})" \
                       r"(?P<start_day>[0-9]{8})_" \
                       r"(?P<start_time>[0-9]{6})_" \
                       r"(?P<duration>[0-9]{8})" \
                       r"(?P<phase>[0-9A-Z]{1})" \
                       r"(?P<cycle>[0-9]{3})_" \
                       r"(?P<relative_orbit>[0-9]{5})_" \
                       r"(?P<absolute_orbit>[0-9]{5})_" \
                       r"(?P<counter>[0-9]{4})\." \
                       r"(?P<satellite_ID>[EN][12])" \
                       r"(?P<extension>(?:\.zip|\.tar\.gz|))$"

        self.pattern_pid = r"(?P<sat_id>(?:SAR|ASA))_" \
                           r"(?P<image_mode>(?:IM(?:S|P|G|M|_)|AP(?:S|P|G|M|_)|WV(?:I|S|W|_)))_" \
                           r"(?P<processing_level>[012B][CP])"

        self.scene = os.path.realpath(scene)

        self.examine()

        match = re.match(re.compile(self.pattern), os.path.basename(self.file))
        if re.search("IM__0", match.group("product_id")):
            raise IOError("product level 0 not supported (yet)")

        self.gdalinfo(self.scene)

        if self.sensor == "ASAR":
            self.polarisations = [getattr(self, x).replace("/", "") for x in self.__dict__.keys() if re.search("TX_RX_POLAR", x)]
        elif self.sensor in ["ERS1", "ERS2"]:
            self.polarisations = ["VV"]

        self.orbit = self.SPH_PASS[0]
        self.start = self.MPH_SENSING_START
        self.stop = self.MPH_SENSING_STOP
        self.spacing = (self.SPH_RANGE_SPACING, self.SPH_AZIMUTH_SPACING)
        self.looks = [self.SPH_RANGE_LOOKS, self.SPH_AZIMUTH_LOOKS]

    def outname_base(self):
        match1 = re.match(re.compile(self.pattern), os.path.basename(self.scene))
        match2 = re.match(re.compile(self.pattern_pid), match1.group("product_id"))
        fields = ("{:_<4}".format(self.sensor),
                  "{:_<4}".format(match2.group("image_mode")),
                  self.orbit,
                  self.start)
        return "_".join(fields)

    def getCorners(self):
        lon = [getattr(self, x) for x in self.__dict__.keys() if re.search("LONG", x)]
        lat = [getattr(self, x) for x in self.__dict__.keys() if re.search("LAT", x)]
        return {"xmin": min(lon), "xmax": max(lon), "ymin": min(lat), "ymax": max(lat)}

    # todo: prevent conversion if target files already exist
    def convert2gamma(self, directory):
        self.gammadir = directory
        outname = os.path.join(directory, self.outname_base())
        if len(self.getGammaImages(directory)) == 0:
            run(["par_ASAR", self.file, outname])
            os.remove(outname+".hdr")
            for item in finder(directory, [os.path.basename(outname)], regex=True):
                ext = ".par" if item.endswith(".par") else ""
                base = os.path.basename(item).strip(ext)
                base = base.replace(".", "_")
                base = base.replace("PRI", "pri")
                base = base.replace("GRD", "grd")
                base = base.replace("SLC", "slc")
                newname = os.path.join(directory, base+ext)
                os.rename(item, newname)
        else:
            raise IOError("scene already processed")

    def calibrate(self, replace=False):
        k_db = {"ASAR": 55., "ERS1": 58.24, "ERS2": 59.75}[self.sensor]
        inc_ref = 90. if self.sensor == "ASAR" else 23.
        # candidates = [x for x in self.getGammaImages(self.gammadir) if not re.search("_(?:cal|grd)$", x)]
        candidates = [x for x in self.getGammaImages(self.gammadir) if re.search("_pri$", x)]
        for image in candidates:
            out = image.replace("pri", "grd")
            run(["radcal_PRI", image, image+".par", out, out+".par", k_db, inc_ref])
            if replace:
                os.remove(image)
                os.remove(image+".par")

    def unpack(self, directory):
        base_file = os.path.basename(self.file).strip("\.zip|\.tar(?:\.gz|)")
        base_dir = os.path.basename(directory.strip("/"))

        outdir = directory if base_file == base_dir else os.path.join(directory, base_file)

        self._unpack(outdir)
# id = identify("/geonfs01_vol1/ve39vem/swos/ASA_APP_1PTDPA20040102_102928_000000162023_00051_09624_0240.N1.zip")
# id = identify("/geonfs01_vol1/ve39vem/swos/SAR_IMP_1PXASI19920419_110159_00000017C083_00323_03975_8482.E1.zip")

# scenes = finder("/geonfs01_vol1/ve39vem/swos", ["*"])
# counter = 0
# for scene in scenes:
#     counter += 1
#     try:
#         x = ESA(scene)
#         if len(x.findfiles(x.pattern)) == 0:
#             print scene
#             print x.findfiles("[EN][12]$")[0]
#             print "---------------------------------"
#     except RuntimeError as rue:
#         print scene
#         print rue
#         print"---------------------------------"
#     # progress = float(counter)/len(scenes)*100
#     # print progress
#     # if progress % 10 == 0:
#     #     print progress

# scenes = finder("/geonfs01_vol1/ve39vem/swos", ["*.tar.gz"])
# for scene in scenes:
#     print scene


# class RS2(ID):
#     def __init__(self, scene):
#
#         raise IOError
#
#         self.pattern = r'^(?:RS2|RSAT2)_(?:OK[0-9]+)_(?:PK[0-9]+)_(?:DK[0-9]+)_' \
#                        r'(?P<beam>[0-9A-Z]+)_' \
#                        r'(?P<date>[0-9]{8})_' \
#                        r'(?P<time>[0-9]{6})_' \
#                        r'(?P<pols>[HV]{2}_' \
#                        r'(?P<level>SLC|SGX|SGF|SCN|SCW|SSG|SPG)$'
#
#         self.sensor = "RS2"
#         self.scene = os.path.realpath(scene)
#         self.gdalinfo(self.scene)
#         self.start = self.ACQUISITION_START_TIME
#         self.incidence = (self.FAR_RANGE_INCIDENCE_ANGLE + self.NEAR_RANGE_INCIDENCE_ANGLE)/2
#         self.spacing = (self.PIXEL_SPACING, self.LINE_SPACING)
#         self.orbit = self.ORBIT_DIRECTION[0]
#
#     def getCorners(self):
#         lat = [x[1][1] for x in self.gcps]
#         lon = [x[1][0] for x in self.gcps]
#         return {"xmin": min(lon), "xmax": max(lon), "ymin": min(lat), "ymax": max(lat)}

# id = identify("/geonfs01_vol1/ve39vem/RS2/RS2_OK53107_PK504800_DK448361_FQ1_20140606_055403_HH_VV_HV_VH_SLC.zip")


# todo: check self.file and self.scene assignment after unpacking
class SAFE(ID):
    def __init__(self, scene, mode="full"):

        self.scene = os.path.realpath(scene)

        self.pattern = r"^(?P<sensor>S1[AB])_" \
                       r"(?P<beam>S1|S2|S3|S4|S5|S6|IW|EW|WV|EN|N1|N2|N3|N4|N5|N6|IM)_" \
                       r"(?P<product>SLC|GRD|OCN)(?:F|H|M|_)_" \
                       r"(?:1|2)" \
                       r"(?P<category>S|A)" \
                       r"(?P<pols>SH|SV|DH|DV)_" \
                       r"(?P<start>[0-9]{8}T[0-9]{6})_" \
                       r"(?P<stop>[0-9]{8}T[0-9]{6})_" \
                       r"(?:[0-9]{6})_" \
                       r"(?:[0-9A-F]{6})_" \
                       r"(?:[0-9A-F]{4})" \
                       r"\.SAFE$"

        self.pattern_ds = r"^s1[ab]-" \
                          r"(?P<swath>s[1-6]|iw[1-3]?|ew[1-5]?|wv[1-2]|n[1-6])-" \
                          r"(?P<product>slc|grd|ocn)-" \
                          r"(?P<pol>hh|hv|vv|vh)-" \
                          r"(?P<start>[0-9]{8}t[0-9]{6})-" \
                          r"(?P<stop>[0-9]{8}t[0-9]{6})-" \
                          r"(?:[0-9]{6})-(?:[0-9a-f]{6})-" \
                          r"(?P<id>[0-9]{3})" \
                          r"\.xml$"

        self.examine()

        match = re.match(re.compile(self.pattern), os.path.basename(self.file))

        if not match:
            raise IOError("folder does not match S1 scene naming convention")
        for key in re.compile(self.pattern).groupindex:
            setattr(self, key, match.group(key))

        self.polarisations = {"SH": ["HH"], "SV": ["VV"], "DH": ["HH", "HV"], "DV": ["VV", "VH"]}[self.pols]

        self.orbit = "D" if float(re.findall("[0-9]{6}", self.start)[1]) < 120000 else "A"
        self.projection = "+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs"

        if mode == "full":
            self.gdalinfo(self.scene)
            self.spacing = (self.PIXEL_SPACING, self.LINE_SPACING)
            # self.orbit = self.ORBIT_DIRECTION[0]

    def calibrate(self, replace=False):
        print "calibration already performed during import"

    def convert2gamma(self, directory):
        if self.compression is not None:
            raise RuntimeError("scene is not yet unpacked")
        if self.product == "OCN":
            raise IOError("Sentinel-1 OCN products are not supported")
        if self.category == "A":
            raise IOError("Sentinel-1 annotation-only products are not supported")

        if not os.path.isdir(directory):
            os.makedirs(directory)

        for xml_ann in finder(os.path.join(self.scene, "annotation"), [self.pattern_ds], regex=True):
            base = os.path.basename(xml_ann)
            match = re.compile(self.pattern_ds).match(base)

            tiff = os.path.join(self.scene, "measurement", base.replace(".xml", ".tiff"))
            xml_cal = os.path.join(self.scene, "annotation", "calibration", "calibration-" + base)
            # todo: investigate what the noise file is for
            # the use of the noise xml file has been found to occasionally cause severe image artifacts of manifold nature and is thus excluded
            # the reason (GAMMA command error vs. bad SAFE xml file entry) is yet to be discovered
            # xml_noise = os.path.join(self.scene, "annotation", "calibration", "noise-" + base)
            xml_noise = "-"
            fields = ("{:_<4}".format(self.sensor),
                      "{:_<4}".format(match.group("swath").upper()),
                      self.orbit,
                      self.start,
                      match.group("pol").upper(),
                      match.group("product"))
            name = os.path.join(directory, "_".join(fields))

            if match.group("product") == "slc":
                cmd = ["par_S1_SLC", tiff, xml_ann, xml_cal, xml_noise, name + ".par", name, name + ".tops_par"]
            else:
                cmd = ["par_S1_GRD", tiff, xml_ann, xml_cal, xml_noise, name + ".par", name]
            try:
                run(cmd)
            except ImportWarning:
                pass

    def getCorners(self):
        if self.compression == "zip":
            with zf.ZipFile(self.scene, "r") as z:
                kml = z.open([x for x in z.namelist() if re.search("map-overlay\.kml", x)][0], "r").read()
        # todo: this looks wrong; check whether it's correct
        # elif self.compression == "tar":
        #     tar = tf.open(self.scene, "r")
        #     kml = tar.extractfile().read()
        #     tar.close()
        else:
            with open(finder(self.scene, ["*map-overlay.kml"])[0], "r") as infile:
                kml = infile.read()
        elements = ElementTree.fromstring(kml).findall(".//coordinates")

        coordinates = [x.split(",") for x in elements[0].text.split()]
        lat = [float(x[1]) for x in coordinates]
        lon = [float(x[0]) for x in coordinates]
        return {"xmin": min(lon), "xmax": max(lon), "ymin": min(lat), "ymax": max(lat)}

    # def getCorners(self):
    #     lat = [x[1][1] for x in self.gcps]
    #     lon = [x[1][0] for x in self.gcps]
    #     return {"xmin": min(lon), "xmax": max(lon), "ymin": min(lat), "ymax": max(lat)}

    def outname_base(self):
        fields = ("{:_<4}".format(self.sensor),
                  "{:_<4}".format(self.beam),
                  self.orbit,
                  self.start)
        return "_".join(fields)

    def unpack(self, directory):
        outdir = os.path.join(directory, os.path.basename(self.file))
        self._unpack(outdir)

# id = identify("/geonfs01_vol1/ve39vem/S1/archive/S1A_EW_GRDM_1SDH_20150408T053103_20150408T053203_005388_006D8D_5FAC.zip")


# todo: remove class and change dependencies to class CEOS (scripts: gammaGUI/reader_ers.py)
# class ERS(object):
#     def __init__(self, scene):
#
#         try:
#             lea = finder(scene, ["LEA_01.001"])[0]
#         except IndexError:
#             raise IOError("wrong input format; no leader file found")
#         with open(lea, "r") as infile:
#             text = infile.read()
#         # extract frame id
#         frame_index = re.search("FRAME=", text).end()
#         self.frame = text[frame_index:frame_index+4]
#         # extract calibration meta information
#         stripper = " \t\r\n\0"
#         self.sensor = text[(720+395):(720+411)].strip(stripper)
#         self.date = int(text[(720+67):(720+99)].strip(stripper)[:8])
#         self.proc_fac = text[(720+1045):(720+1061)].strip(stripper)
#         self.proc_sys = text[(720+1061):(720+1069)].strip(stripper)
#         self.proc_vrs = text[(720+1069):(720+1077)].strip(stripper)
#         text_subset = text[re.search("FACILITY RELATED DATA RECORD \[ESA GENERAL TYPE\]", text).start()-13:]
#         self.cal = -10*math.log(float(text_subset[663:679].strip(stripper)), 10)
#         self.antenna_flag = text_subset[659:663].strip(stripper)

        # the following section is only relevant for PRI products and can be considered future work
        # select antenna gain correction lookup file from extracted meta information
        # the lookup files are stored in a subfolder CAL which is included in the pythonland software package
        # if sensor == "ERS1":
        #     if date < 19950717:
        #         antenna = "antenna_ERS1_x_x_19950716"
        #     else:
        #         if proc_sys == "VMP":
        #             antenna = "antenna_ERS2_VMP_v68_x" if proc_vrs >= 6.8 else "antenna_ERS2_VMP_x_v67"
        #         elif proc_fac == "UKPAF" and date < 19970121:
        #             antenna = "antenna_ERS1_UKPAF_19950717_19970120"
        #         else:
        #             antenna = "antenna_ERS1"
        # else:
        #     if proc_sys == "VMP":
        #         antenna = "antenna_ERS2_VMP_v68_x" if proc_vrs >= 6.8 else "antenna_ERS2_VMP_x_v67"
        #     elif proc_fac == "UKPAF" and date < 19970121:
        #         antenna = "antenna_ERS2_UKPAF_x_19970120"
        #     else:
        #         antenna = "antenna_ERS2"
