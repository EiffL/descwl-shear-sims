import numpy as np
import os
import copy
import galsim
import descwl

from .shifts import get_shifts, get_pair_shifts
from .constants import SCALE
from .cache_tools import cached_catalog_read


DEFAULT_FIXED_GAL_CONFIG = {
    "mag": 17.0,
    "hlr": 0.5,
    "morph": "exp",
}


def make_galaxy_catalog(
    *,
    rng,
    gal_type,
    coadd_dim=None,
    buff=None,
    layout=None,
    gal_config=None,
    sep=None,
):
    """
    rng: numpy.random.RandomState
        Numpy random state
    gal_type: string
        'fixed' or 'wldeblend'
    coadd_dim: int
        Dimensions of coadd
    buff: int
        Buffer around the edge where no objects are drawn
    layout: string, optional
        'grid' or 'random'.  Ignored for gal_type "wldeblend", otherwise
        required.
    gal_config: dict or None
        Can be sent for fixed galaxy catalog.  See DEFAULT_FIXED_GAL_CONFIG
        for defaults mag, hlr and morph
    sep: float, optional
        Separation of pair in arcsec for layout='pair'
    """
    if layout == 'pair':
        if sep is None:
            raise ValueError(
                f'send sep= for gal_type {gal_type} and layout {layout}'
            )
        gal_config = get_fixed_gal_config(config=gal_config)
        galaxy_catalog = FixedPairGalaxyCatalog(
            rng=rng,
            mag=gal_config['mag'],
            hlr=gal_config['hlr'],
            morph=gal_config['morph'],
            sep=sep,
        )
    else:
        if coadd_dim is None:
            raise ValueError(
                f'send coadd_dim= for gal_type {gal_type} and layout {layout}'
            )
        if buff is None:
            raise ValueError(
                f'send buff= for gal_type {gal_type} and layout {layout}'
            )

        if gal_type == 'wldeblend':
            galaxy_catalog = WLDeblendGalaxyCatalog(
                rng=rng,
                coadd_dim=coadd_dim,
                buff=buff,
            )
        else:

            if layout is None:
                raise ValueError("send layout= for gal_type '%s'" % gal_type)

            gal_config = get_fixed_gal_config(config=gal_config)
            galaxy_catalog = FixedGalaxyCatalog(
                rng=rng,
                coadd_dim=coadd_dim,
                buff=buff,
                layout=layout,
                mag=gal_config['mag'],
                hlr=gal_config['hlr'],
                morph=gal_config['morph'],
            )

    return galaxy_catalog


def get_fixed_gal_config(config=None):
    """
    get the configuration for fixed galaxies, with defaults in place

    Parameters
    ----------
    config: dict, optional
        The input config. Over-rides defaults

    Returns
    -------
    the config dict
    """
    out_config = copy.deepcopy(DEFAULT_FIXED_GAL_CONFIG)

    if config is not None:
        for key in config:
            if key not in out_config:
                raise ValueError("bad key for fixed gals: '%s'" % key)
        out_config.update(config)
    return out_config


class FixedGalaxyCatalog(object):
    """
    Galaxies of fixed galsim type, flux, and size.  For the type
    bdk the ellipticity varies.

    Same for all bands

    Parameters
    ----------
    rng: np.random.RandomState
        The random number generator
    coadd_dim: int
        dimensions of the coadd
    buff: int
        Buffer region with no objects, on all sides of image
    layout: string
        The layout of objects, either 'grid' or 'random'
    mag: float
        Magnitude of all objects. Objects brighter than magntiude 17 (e.g., 14
        since mags are opposite) tend to cause the Rubin Observatory science
        pipeline detection algorithm to misdetect isolted objects in unphysical
        ways. This effect causes the shear response to be non-linear and so
        metadetect will fail. For this reason, you should use the default
        magnitude of 17 or fainter for this kind of galaxy.
    hlr: float
        Half light radius of all objects
    morph: str
        Galaxy morphology, 'exp', 'dev' or 'bd', 'bdk'.  Default 'exp'
    """
    def __init__(self, *, rng, coadd_dim, buff, layout, mag, hlr, morph='exp'):
        self.gal_type = 'fixed'
        self.morph = morph
        self.mag = mag
        self.hlr = hlr
        self.morph_seed = rng.randint(0, 2**31)
        self.gs_morph_seed = rng.randint(0, 2**31)

        self.shifts_array = get_shifts(
            rng=rng,
            coadd_dim=coadd_dim,
            buff=buff,
            layout=layout,
        )

    def __len__(self):
        return len(self.shifts_array)

    def get_objlist(self, *, survey):
        """
        get a list of galsim objects

        Parameters
        ----------
        band: string
            Get objects for this band.  For the fixed
            catalog, the objects are the same for every band

        Returns
        -------
        [galsim objects], [shifts]
        """

        self._morph_rng = np.random.RandomState(self.morph_seed)
        self._gs_morph_rng = galsim.BaseDeviate(seed=self.gs_morph_seed)

        flux = survey.get_flux(self.mag)

        sarray = self.shifts_array
        objlist = []
        shifts = []
        for i in range(len(self)):
            objlist.append(self._get_galaxy(flux))
            shifts.append(galsim.PositionD(sarray['dx'][i], sarray['dy'][i]))

        return objlist, shifts

    def _get_galaxy(self, flux):
        """
        get a galaxy object

        Parameters
        ----------
        i: int
            Index of object
        flux: float
            Flux of object

        Returns
        --------
        galsim.GSObject
        """

        if self.morph == 'exp':
            gal = galsim.Exponential(
                half_light_radius=self.hlr,
                flux=flux,
            )
        elif self.morph == 'dev':
            gal = galsim.DeVaucouleurs(
                half_light_radius=self.hlr,
                flux=flux,
            )
        elif self.morph == 'bd':
            gal = _generate_bd(
                rng=self._morph_rng,
                hlr=self.hlr,
                flux=flux,
            )
        elif self.morph == 'bdk':
            gal = _generate_bdk(
                rng=self._morph_rng,
                gsrng=self._gs_morph_rng,
                hlr=self.hlr,
                flux=flux,
            )
        else:
            raise ValueError(f"bad gal type '{self.morph}'")

        return gal


def _generate_bd(
    rng, hlr, flux,
    max_bulge_shift_frac=0.1,  # fraction of hlr
    max_bulge_rot=np.pi/4,
):

    bulge_shift = rng.uniform(low=0.0, high=max_bulge_shift_frac*hlr)
    bulge_offangle = rng.uniform(low=0, high=2*np.pi)
    bulge_shiftx = bulge_shift * np.cos(bulge_offangle)
    bulge_shifty = bulge_shift * np.sin(bulge_offangle)

    bulge_rot = rng.uniform(low=-max_bulge_rot, high=max_bulge_rot/4)

    g1disk, g2disk = _generate_g1g2(rng)

    g1bulge, g2bulge = _rotate_shape(g1disk, g2disk, bulge_rot)

    bulge_frac = rng.uniform(low=0.0, high=1.0)
    disk_frac = (1.0 - bulge_frac)

    bulge = galsim.DeVaucouleurs(
        half_light_radius=hlr,
        flux=flux * bulge_frac,
    ).shear(
        g1=g1bulge, g2=g2bulge,
    ).shift(
        bulge_shiftx, bulge_shifty,
    )

    disk = galsim.Exponential(
        half_light_radius=hlr,
        flux=flux * disk_frac,
    ).shear(
        g1=g1disk, g2=g2disk,
    )

    return galsim.Add(bulge, disk)


def _generate_bdk(
    rng, gsrng, hlr, flux,
    knots_hlr_frac=0.25,
    max_knots_disk_frac=0.1,  # fraction of disk light
    max_bulge_shift_frac=0.1,  # fraction of hlr
    max_bulge_rot=np.pi/4,
):
    knots_hlr = knots_hlr_frac * hlr
    knots_sub_frac = rng.uniform(low=0.0, high=max_knots_disk_frac)

    bulge_shift = rng.uniform(low=0.0, high=max_bulge_shift_frac*hlr)
    bulge_offangle = rng.uniform(low=0, high=2*np.pi)
    bulge_shiftx = bulge_shift * np.cos(bulge_offangle)
    bulge_shifty = bulge_shift * np.sin(bulge_offangle)

    bulge_rot = rng.uniform(low=-max_bulge_rot, high=max_bulge_rot/4)

    g1disk, g2disk = _generate_g1g2(rng)

    g1bulge, g2bulge = _rotate_shape(g1disk, g2disk, bulge_rot)

    bulge_frac = rng.uniform(low=0.0, high=1.0)
    all_disk_frac = (1.0 - bulge_frac)

    disk_frac = (1 - knots_sub_frac) * all_disk_frac
    knots_frac = knots_sub_frac * all_disk_frac

    bulge = galsim.DeVaucouleurs(
        half_light_radius=hlr,
        flux=flux * bulge_frac,
    ).shear(
        g1=g1bulge, g2=g2bulge,
    ).shift(
        bulge_shiftx, bulge_shifty,
    )

    disk = galsim.Exponential(
        half_light_radius=hlr,
        flux=flux * disk_frac,
    ).shear(
        g1=g1disk, g2=g2disk,
    )
    knots = galsim.RandomKnots(
        npoints=10,
        half_light_radius=knots_hlr,
        flux=flux * knots_frac,
        rng=gsrng,
    ).shear(
        g1=g1disk, g2=g2disk,
    )

    return galsim.Add(bulge, disk, knots)


def _generate_g1g2(rng, std=0.2):
    while True:
        g1, g2 = rng.normal(scale=std, size=2)
        g = np.sqrt(g1**2 + g2**2)
        if abs(g) < 0.9999:
            break

    return g1, g2


def _rotate_shape(g1, g2, theta_radians):
    twotheta = 2.0 * theta_radians

    cos2angle = np.cos(twotheta)
    sin2angle = np.sin(twotheta)
    g1rot = g1 * cos2angle + g2 * sin2angle
    g2rot = -g1 * sin2angle + g2 * cos2angle

    return g1rot, g2rot


class FixedPairGalaxyCatalog(FixedGalaxyCatalog):
    """
    A pair of galaxies of fixed galsim type, flux, and size

    Same for all bands

    Parameters
    ----------
    rng: np.random.RandomState
        The random number generator
    mag: float
        Magnitude of all objects. Objects brighter than magntiude 17 (e.g., 14
        since mags are opposite) tend to cause the Rubin Observatory science
        pipeline detection algorithm to misdetect isolted objects in unphysical
        ways. This effect causes the shear response to be non-linear and so
        metadetect will fail. For this reason, you should use the default
        magnitude of 17 or fainter for this kind of galaxy.
    hlr: float
        Half light radius of all objects
    sep: float
        Separation of pair in arcsec
    morph: str
        Galaxy morphology, 'exp', 'dev' or 'bd', 'bdk'.  Default 'exp'
    """
    def __init__(self, *, rng, mag, hlr, sep, morph='exp'):
        self.gal_type = 'fixed'
        self.morph = morph
        self.mag = mag
        self.hlr = hlr
        self.rng = rng

        self.morph_seed = rng.randint(0, 2**31)
        self.gs_morph_seed = rng.randint(0, 2**31)

        self.shifts_array = get_pair_shifts(
            rng=rng,
            sep=sep,
        )


class WLDeblendGalaxyCatalog(object):
    """
    Catalog of galaxies from wldeblend

    Parameters
    ----------
    rng: np.random.RandomState
        The random number generator
    coadd_dim: int
        Dimensions of the coadd
    buff: int
        Buffer region with no objects, on all sides of image
    """
    def __init__(self, *, rng, coadd_dim, buff):
        self.gal_type = 'wldeblend'
        self.rng = rng

        self._wldeblend_cat = read_wldeblend_cat(rng)

        # one square degree catalog, convert to arcmin
        gal_dens = self._wldeblend_cat.size / (60 * 60)
        area = ((coadd_dim - 2*buff)*SCALE/60)**2
        nobj_mean = area * gal_dens
        nobj = rng.poisson(nobj_mean)

        self.shifts_array = get_shifts(
            rng=rng,
            coadd_dim=coadd_dim,
            buff=buff,
            layout="random",
            nobj=nobj,
        )

        num = len(self)
        self.indices = self.rng.randint(
            0,
            self._wldeblend_cat.size,
            size=num,
        )

        self.angles = self.rng.uniform(low=0, high=360, size=num)

    def __len__(self):
        return len(self.shifts_array)

    def get_objlist(self, *, survey):
        """
        get a list of galsim objects

        Parameters
        ----------
        survey: WLDeblendSurvey
            The survey object

        Returns
        -------
        [galsim objects], [shifts]
        """

        builder = descwl.model.GalaxyBuilder(
            survey=survey.descwl_survey,
            no_disk=False,
            no_bulge=False,
            no_agn=False,
            verbose_model=False,
        )

        band = survey.filter_band

        sarray = self.shifts_array
        objlist = []
        shifts = []
        for i in range(len(self)):
            objlist.append(self._get_galaxy(builder, band, i))
            shifts.append(galsim.PositionD(sarray['dx'][i], sarray['dy'][i]))

        return objlist, shifts

    def _get_galaxy(self, builder, band, i):
        """
        Get a galaxy

        Parameters
        ----------
        builder: descwl.model.GalaxyBuilder
            Builder for this object
        band: string
            Band string, e.g. 'r'
        i: int
            Index of object

        Returns
        -------
        galsim.GSObject
        """
        index = self.indices[i]

        angle = self.angles[i]

        galaxy = builder.from_catalog(
            self._wldeblend_cat[index],
            0,
            0,
            band,
        ).model.rotate(
            angle * galsim.degrees,
        )

        return galaxy


def read_wldeblend_cat(rng):
    """
    Read the catalog from the cache, but update the position angles each time

    Parameters
    ----------
    rng: np.random.RandomState
        The random number generator

    Returns
    -------
    array with fields
    """
    fname = os.path.join(
        os.environ.get('CATSIM_DIR', '.'),
        'OneDegSq.fits',
    )

    # not thread safe
    cat = cached_catalog_read(fname)
    return cat
